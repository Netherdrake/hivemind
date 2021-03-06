import json
import logging
import glob
import time
import re
import os

from hive.db.methods import query_one, query, query_row, db_last_block
from steem.steemd import Steemd
from steem.utils import json_expand
from toolz import partition_all

log = logging.getLogger(__name__)

from hive.indexer.cache import cache_missing_posts, rebuild_cache, select_paidout_posts, update_posts_batch
from hive.indexer.community import process_json_community_op, create_post_as


STEEMD_URL = os.environ.get('STEEMD_URL')

# core
# ----
def is_valid_account_name(name):
    return re.match('^[a-z][a-z0-9\-.]{2,15}$', name)


def get_account_id(name):
    if is_valid_account_name(name):
        return query_one("SELECT id FROM hive_accounts "
                "WHERE name = '%s' LIMIT 1" % name)


def get_post_id_and_depth(author, permlink):
    res = None
    if author:
        res = query_row("SELECT id, depth FROM hive_posts WHERE "
                "author = '%s' AND permlink = '%s'" % (author, permlink))
    return res or (None, -1)


def urls_to_tuples(urls):
    tuples = []
    for url in urls:
        author, permlink = url.split('/')
        id, is_deleted = query_row("SELECT id,is_deleted FROM hive_posts "
                "WHERE author = '%s' AND permlink = '%s'" % (author, permlink))
        if not id:
            raise Exception("Post not found! {}/{}".format(author, permlink))
        if is_deleted:
            continue
        tuples.append([id, author, permlink])
    return tuples


# given a comment op, safely read 'community' field from json
def get_op_community(comment):
    if not comment['json_metadata']:
        return None
    md = None
    try:
        md = json.loads(comment['json_metadata'])
    except:
        return None
    if type(md) is not dict or 'community' not in md:
        return None
    return md['community']


# block-level routines
# --------------------

# register any new accounts in a block
def register_accounts(accounts, date):
    for account in set(accounts):
        if not get_account_id(account):
            query("INSERT INTO hive_accounts (name, created_at) "
                    "VALUES ('%s', '%s')" % (account, date))


# marks posts as deleted and removes them from feed cache
def delete_posts(ops):
    for op in ops:
        post_id, depth = get_post_id_and_depth(op['author'], op['permlink'])
        query("UPDATE hive_posts SET is_deleted = 1 WHERE id = :id", id=post_id)
        query("DELETE FROM hive_posts_cache WHERE post_id = :id", id=post_id)
        query("DELETE FROM hive_feed_cache WHERE post_id = :id", id=post_id)


# registers new posts (not edits), inserts into feed cache
def register_posts(ops, date):
    for op in ops:
        sql = ("SELECT id, is_deleted FROM hive_posts "
            "WHERE author = '%s' AND permlink = '%s'")
        ret = query_row(sql % (op['author'], op['permlink']))
        id = None
        if ret:
            if ret[1] == 0:
                # if post has id and is_deleted=0, then it's an edit -- ignore.
                continue
            else:
                id = ret[0]

        # set parent & inherited attributes
        if op['parent_author'] == '':
            parent_id = None
            depth = 0
            category = op['parent_permlink']
            community = get_op_community(op) or op['author']
        else:
            parent_data = query_row("SELECT id, depth, category, community FROM hive_posts WHERE author = '%s' "
                                      "AND permlink = '%s'" % (op['parent_author'], op['parent_permlink']))
            parent_id, parent_depth, category, community = parent_data
            depth = parent_depth + 1

        # validated community; will return None if invalid & defaults to author.
        community = create_post_as(community, op) or op['author']

        # if we're reusing a previously-deleted post (rare!), update it
        if id:
            query("UPDATE hive_posts SET is_deleted = 0, parent_id = %s, category = '%s', community = '%s', depth = %d WHERE id = %d" % (parent_id or 'NULL', category, community, depth, id))
            query("DELETE FROM hive_feed_cache WHERE account = :account AND post_id = :id", account=op['author'], id=id)
        else:
            query("INSERT INTO hive_posts (parent_id, author, permlink, category, community, depth, created_at) "
                  "VALUES (%s, '%s', '%s', '%s', '%s', %d, '%s')" % (
                      parent_id or 'NULL', op['author'], op['permlink'], category, community, depth, date))
            id = query_one("SELECT id FROM hive_posts WHERE author = '%s' AND permlink = '%s'" % (op['author'], op['permlink']))

        # add top-level posts to feed cache
        if depth == 0:
            sql = "INSERT INTO hive_feed_cache (account, post_id, created_at) VALUES (:account, :id, :created_at)"
            query(sql, account=op['author'], id=id, created_at=date)



def process_json_follow_op(account, op_json, block_date):
    """ Process legacy 'follow' plugin ops (follow/mute/clear, reblog) """
    if type(op_json) != list:
        return
    if len(op_json) != 2:
        return
    if first(op_json) not in ['follow', 'reblog']:
        return
    if not isinstance(second(op_json), dict):
        return

    cmd, op_json = op_json  # ['follow', {data...}]
    if cmd == 'follow':
        if type(op_json['what']) != list:
            return
        what = first(op_json['what']) or 'clear'
        if what not in ['blog', 'clear', 'ignore']:
            return
        if not all([key in op_json for key in ['follower', 'following']]):
            print("bad follow op: {} {}".format(block_date, op_json))
            return

        follower = op_json['follower']
        following = op_json['following']

        if follower != account:
            return  # impersonation
        if not all(filter(is_valid_account_name, [follower, following])):
            return  # invalid input

        sql = """
        INSERT IGNORE INTO hive_follows (follower, following, created_at, state)
        VALUES (:fr, :fg, :at, :state) ON DUPLICATE KEY UPDATE state = :state
        """
        state = {'clear': 0, 'blog': 1, 'ignore': 2}[what]
        query(sql, fr=follower, fg=following, at=block_date, state=state)

    elif cmd == 'reblog':
        blogger = op_json['account']
        author = op_json['author']
        permlink = op_json['permlink']

        if blogger != account:
            return  # impersonation
        if not all(filter(is_valid_account_name, [author, blogger])):
            return

        post_id, depth = get_post_id_and_depth(author, permlink)

        if depth > 0:
            return  # prevent comment reblogs

        if not post_id:
            print("reblog: post not found: {}/{}".format(author, permlink))
            return

        if 'delete' in op_json and op_json['delete'] == 'delete':
            query("DELETE FROM hive_reblogs WHERE account = '%s' AND post_id = %d LIMIT 1" % (blogger, post_id))
            sql = "DELETE FROM hive_feed_cache WHERE account = :account AND post_id = :id"
            query(sql, account=blogger, id=post_id)
        else:
            query("INSERT IGNORE INTO hive_reblogs (account, post_id, created_at) "
                  "VALUES ('%s', %d, '%s')" % (blogger, post_id, block_date))
            sql = "INSERT IGNORE INTO hive_feed_cache (account, post_id, created_at) VALUES (:account, :id, :created_at)"
            query(sql, account=blogger, id=post_id, created_at=block_date)


# process a single block. always wrap in a transaction!
def process_block(block, is_initial_sync = False):
    date = block['timestamp']
    block_id = block['block_id']
    prev = block['previous']
    block_num = int(block_id[:8], base=16)
    txs = block['transactions']

    query("INSERT INTO hive_blocks (num, hash, prev, txs, created_at) "
          "VALUES (%d, '%s', '%s', %d, '%s')" % (block_num, block_id, prev, len(txs), date))

    accounts = set()
    comments = []
    json_ops = []
    deleted = []
    dirty = set()
    for tx in txs:
        for operation in tx['operations']:
            op_type, op = operation

            if op_type == 'pow':
                accounts.add(op['worker_account'])
            elif op_type == 'pow2':
                accounts.add(op['work'][1]['input']['worker_account'])
            elif op_type in ['account_create', 'account_create_with_delegation']:
                accounts.add(op['new_account_name'])
            elif op_type == 'comment':
                comments.append(op)
                dirty.add(op['author']+'/'+op['permlink'])
            elif op_type == 'delete_comment':
                deleted.append(op)
            elif op_type == 'custom_json':
                json_ops.append(op)
            elif op_type == 'vote':
                dirty.add(op['author']+'/'+op['permlink'])

    register_accounts(accounts, date)  # if an account does not exist, mark it as created in this block
    register_posts(comments, date)  # if this is a new post, add the entry and validate community param
    delete_posts(deleted)  # mark hive_posts.is_deleted = 1

    for op in map(json_expand, json_ops):
        if op['id'] not in ['follow', 'com.steemit.community']:
            continue

        # we are assuming `required_posting_auths` is always used and length 1.
        # it may be that some ops will require `required_active_auths` instead
        # (e.g. if we use that route for admin action of acct creation)
        # if op['required_active_auths']:
        #    log.warning("unexpected active auths: %s" % op)
        if len(op['required_posting_auths']) != 1:
            log.warning("unexpected auths: %s" % op)
            continue

        account = op['required_posting_auths'][0]
        op_json = op['json']

        if op['id'] == 'follow':
            if block_num < 6000000 and type(op_json) != list:
                op_json = ['follow', op_json]  # legacy compat
            process_json_follow_op(account, op_json, date)
        elif op['id'] == 'com.steemit.community':
            if block_num > 13e6:
                process_json_community_op(account, op_json, date)

    # return all posts modified this block
    return dirty


# batch-process blocks, wrap in a transaction
def process_blocks(blocks, is_initial_sync = False):
    dirty = set()
    query("START TRANSACTION")
    for block in blocks:
        dirty |= process_block(block, is_initial_sync)
    query("COMMIT")
    return dirty



# sync routines
# -------------

def sync_from_checkpoints(is_initial_sync):
    last_block = db_last_block()

    fn = lambda f: [int(f.split('/')[-1].split('.')[0]), f]
    mydir = os.path.dirname(os.path.realpath(__file__ + "/../.."))
    files = map(fn, glob.glob(mydir + "/checkpoints/*.json.lst"))
    files = sorted(files, key = lambda f: f[0])

    last_read = 0
    for (num, path) in files:
        if last_block < num:
            print("[SYNC] Load {} -- last block: {}".format(path, last_block))
            skip_lines = last_block - last_read
            sync_from_file(path, skip_lines, 250, is_initial_sync)
            last_block = num
        last_read = num


def sync_from_file(file_path, skip_lines, chunk_size=250, is_initial_sync=False):
    with open(file_path) as f:
        # each line in file represents one block
        # we can skip the blocks we already have
        remaining = drop(skip_lines, f)
        for batch in partition_all(chunk_size, remaining):
            process_blocks(map(json.loads, batch), is_initial_sync)


def sync_from_steemd(is_initial_sync):
    if STEEMD_URL:
        steemd = Steemd(nodes=[STEEMD_URL])
    else:
        steemd = Steemd()
    dirty = set()

    lbound = db_last_block() + 1
    ubound = steemd.last_irreversible_block_num

    print("[SYNC] {} blocks to batch sync".format(ubound - lbound + 1))

    if not is_initial_sync:
        query("START TRANSACTION")

    start_num = lbound
    start_time = time.time()
    while lbound < ubound:
        to = min(lbound + 1000, ubound)
        blocks = steemd.get_blocks_range(lbound, to)
        lbound = to
        dirty |= process_blocks(blocks, is_initial_sync)

        rate = (lbound - start_num) / (time.time() - start_time)
        print("[SYNC] Got block {} ({}/s) {}m remaining".format(
            to - 1, round(rate, 1), round((ubound-lbound) / rate / 60, 2)))

    # batch update post cache after catching up to head block
    if not is_initial_sync:
        date = steemd.get_dynamic_global_properties()['time']

        print("[PREP] Update {} edited posts".format(len(dirty), date))
        update_posts_batch(urls_to_tuples(dirty), steemd, date)

        paidout = select_paidout_posts(date)
        print("[PREP] Process {} payouts".format(len(paidout)))
        update_posts_batch(paidout, steemd, date)

        query("COMMIT")


def listen_steemd(trail_blocks = 2):
    steemd = Steemd()
    curr_block = db_last_block()
    head_block = steemd.get_dynamic_global_properties()['head_block_number']
    last_hash = False

    while True:
        curr_block = curr_block + 1

        # pause for a block interval if trailing too close
        while curr_block > head_block - trail_blocks:
            time.sleep(3)
            head_block += 1

        # get the target block; if DNE, pause and retry
        block = steemd.get_block(curr_block)
        while not block:
            time.sleep(3)
            head_block += 1
            block = steemd.get_block(curr_block)

        num = int(block['block_id'][:8], base=16)
        print("[LIVE] Got block {} at {} with {} txs -- ".format(num,
            block['timestamp'], len(block['transactions'])), end='')

        # ensure the block we received links to our last
        if last_hash and last_hash != block['previous']:
            # this condition is very rare unless trail_blocks is 0 and fork is
            # encountered; to handle gracefully, implement a pop_block method
            raise Exception("Unlinkable block: have {}, got {} -> {})".format(
                last_hash, block['previous'], block['block_id']))
        last_hash = block['block_id']

        query("START TRANSACTION")

        dirty = process_block(block)
        update_posts_batch(urls_to_tuples(dirty), steemd, block['timestamp'])

        paidout = select_paidout_posts(block['timestamp'])
        update_posts_batch(paidout, steemd, block['timestamp'])

        print("{} edits, {} payouts".format(len(dirty), len(paidout)))
        query("COMMIT")


def run():
    # if tables not created, do so now
    if not query_row('SHOW TABLES'):
        print("No tables found. Initializing db...")
        setup()

    #TODO: if initial sync is interrupted, cache never rebuilt
    #TODO: do not build partial feed_cache during init_sync
    # if this is the initial sync, batch updates until very end
    is_initial_sync = not query_one("SELECT 1 FROM hive_posts_cache LIMIT 1")

    if is_initial_sync:
        print("*** Initial sync ***")
    else:
        # perform cleanup in case process did not exit cleanly
        cache_missing_posts()

    # fast-load checkpoint files
    sync_from_checkpoints(is_initial_sync)

    # fast-load from steemd
    sync_from_steemd(is_initial_sync)

    # upon completing initial sync, perform some batch processing
    if is_initial_sync:
        rebuild_cache()

    # initialization complete. follow head blocks
    listen_steemd()


def head_state(*args):
    _ = args  # JSONRPC injects 4 arguments here
    steemd_head = Steemd().last_irreversible_block_num
    hive_head = db_last_block()
    diff = steemd_head - hive_head
    return dict(steemd=steemd_head, hive=hive_head, diff=diff)


if __name__ == '__main__':
    run()
