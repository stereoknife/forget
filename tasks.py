from celery import Celery, Task

from app import app as flaskapp
from app import db
from model import Session, Account, TwitterArchive, Post, OAuthToken, MastodonInstance
import lib.twitter
import lib.mastodon
from mastodon.Mastodon import MastodonRatelimitError
from twitter import TwitterError
from urllib.error import URLError
from datetime import timedelta, datetime
from zipfile import ZipFile
from io import BytesIO, TextIOWrapper
import json
from kombu import Queue
import random
import version

app = Celery('tasks', broker=flaskapp.config['CELERY_BROKER'], task_serializer='pickle')
app.conf.task_queues = (
        Queue('default', routing_key='celery'),
        Queue('high_prio', routing_key='high'),
        Queue('higher_prio', routing_key='higher'),
)
app.conf.task_default_queue = 'default'
app.conf.task_default_exchange = 'celery'
app.conf.task_default_exchange_type = 'direct'

if 'SENTRY_DSN' in flaskapp.config:
    from raven import Client
    from raven.contrib.celery import register_signal, register_logger_signal
    sentry = Client(flaskapp.config['SENTRY_DSN'], release=version.version)
    register_logger_signal(sentry)
    register_signal(sentry)


class DBTask(Task):
    def __call__(self, *args, **kwargs):
        try:
            super().__call__(*args, **kwargs)
        finally:
            db.session.close()

app.Task = DBTask

@app.task(autoretry_for=(TwitterError, URLError, MastodonRatelimitError))
def fetch_acc(id, cursor=None):
    acc = Account.query.get(id)
    print(f'fetching {acc}')
    try:
        action = lambda acc, cursor: None
        if(acc.service == 'twitter'):
            action = lib.twitter.fetch_acc
        elif(acc.service == 'mastodon'):
            action = lib.mastodon.fetch_acc
        cursor = action(acc, cursor)
        if cursor:
            fetch_acc.si(id, cursor).apply_async()
    finally:
        db.session.rollback()
        acc.touch_fetch()
        db.session.commit()

@app.task
def queue_fetch_for_most_stale_accounts(min_staleness=timedelta(minutes=5), limit=20):
    accs = Account.query\
            .join(Account.tokens).group_by(Account)\
            .filter(Account.last_fetch < db.func.now() - min_staleness)\
            .order_by(db.asc(Account.last_fetch))\
            .limit(limit)
    for acc in accs:
        fetch_acc.s(acc.id).delay()
        #acc.touch_fetch()
    db.session.commit()


@app.task(autoretry_for=(TwitterError, URLError))
def import_twitter_archive_month(archive_id, month_path):
    ta = TwitterArchive.query.get(archive_id)

    try:

        with ZipFile(BytesIO(ta.body), 'r') as zipfile:
            with TextIOWrapper(zipfile.open(month_path, 'r')) as f:

                # seek past header
                f.readline()

                tweets = json.load(f)

        for tweet in tweets:
            post = lib.twitter.post_from_api_tweet_object(tweet)
            existing_post = db.session.query(Post).get(post.id)

            if post.author_id != ta.account_id \
            or existing_post and existing_post.author_id != ta.account_id:
                raise Exception("Shenanigans!")

            post = db.session.merge(post)

        ta.chunks_successful = TwitterArchive.chunks_successful + 1
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        ta.chunks_failed = TwitterArchive.chunks_failed + 1
        db.session.commit()
        raise e


@app.task
def periodic_cleanup():
    # delete sessions after 48 hours
    Session.query.filter(Session.updated_at < (db.func.now() - timedelta(hours=48))).\
            delete(synchronize_session=False)

    # delete twitter archives after 3 days
    TwitterArchive.query.filter(TwitterArchive.updated_at < (db.func.now() - timedelta(days=3))).\
            delete(synchronize_session=False)

    # delete anonymous oauth tokens after 1 day
    OAuthToken.query.filter(OAuthToken.updated_at < (db.func.now() - timedelta(days=1)))\
            .filter(OAuthToken.account_id == None)\
            .delete(synchronize_session=False)

    # disable users with no tokens
    unreachable = Account.query.outerjoin(Account.tokens).group_by(Account).having(db.func.count(OAuthToken.token) == 0).filter(Account.policy_enabled == True)
    for account in unreachable:
        account.policy_enabled = False

    # normalise mastodon instance popularity scores
    biggest_instance = MastodonInstance.query.order_by(db.desc(MastodonInstance.popularity)).first()
    if biggest_instance.popularity > 40:
        MastodonInstance.query.update({MastodonInstance.popularity: MastodonInstance.popularity * 40 / biggest_instance.popularity})

    db.session.commit()

@app.task
def queue_deletes():
    eligible_accounts = Account.query.filter(Account.policy_enabled == True).\
            filter(Account.next_delete < db.func.now())
    for account in eligible_accounts:
        delete_from_account.s(account.id).apply_async()

@app.task(autoretry_for=(TwitterError, URLError, MastodonRatelimitError))
def delete_from_account(account_id):
    account = Account.query.get(account_id)
    latest_n_posts = Post.query.with_parent(account).order_by(db.desc(Post.created_at)).limit(account.policy_keep_latest)
    posts = Post.query.with_parent(account).\
        filter(Post.created_at + account.policy_keep_younger <= db.func.now()).\
        except_(latest_n_posts).\
        order_by(db.func.random()).limit(100).all()

    eligible = None

    action = lambda post: None
    if account.service == 'twitter':
        action = lib.twitter.delete
        posts = refresh_posts(posts)
        eligible = list((post for post in posts if
            (not account.policy_keep_favourites or not post.favourite)
            and (not account.policy_keep_media or not post.has_media)
            ))
    elif account.service == 'mastodon':
        action = lib.mastodon.delete
        for post in posts:
            refreshed = refresh_posts((post,))
            if refreshed and \
            (not account.policy_keep_favourites or not post.favourite) \
            and (not account.policy_keep_media or not post.has_media)\
            and (not account.policy_keep_direct or not post.direct):
                eligible = refreshed
                break

    if eligible:
        if account.policy_delete_every == timedelta(0) and len(eligible) > 1:
            print("deleting all {} eligible posts for {}".format(len(eligible), account))
            for post in eligible:
                account.touch_delete()
                action(post)
        else:
            post = random.choice(eligible) # nosec
            print("deleting {}".format(post))
            account.touch_delete()
            action(post)

    db.session.commit()

def refresh_posts(posts):
    posts = list(posts)
    if len(posts) == 0:
        return []

    if posts[0].service == 'twitter':
        return lib.twitter.refresh_posts(posts)
    elif posts[0].service == 'mastodon':
        return lib.mastodon.refresh_posts(posts)

@app.task(autoretry_for=(TwitterError, URLError), throws=(MastodonRatelimitError))
def refresh_account(account_id):
    account = Account.query.get(account_id)

    limit = 100
    if account.service == 'mastodon':
        limit = 5
    posts = Post.query.with_parent(account).order_by(db.asc(Post.updated_at)).limit(limit).all()

    posts = refresh_posts(posts)
    account.touch_refresh()
    db.session.commit()

@app.task(autoretry_for=(TwitterError, URLError), throws=(MastodonRatelimitError))
def refresh_account_with_oldest_post():
    post = Post.query.outerjoin(Post.author).join(Account.tokens).group_by(Post).order_by(db.asc(Post.updated_at)).first()
    refresh_account(post.author_id)

@app.task(autoretry_for=(TwitterError, URLError), throws=(MastodonRatelimitError))
def refresh_account_with_longest_time_since_refresh():
    acc = Account.query.join(Account.tokens).group_by(Account).order_by(db.asc(Account.last_refresh)).first()
    refresh_account(acc.id)


app.add_periodic_task(6*60, periodic_cleanup)
app.add_periodic_task(45, queue_fetch_for_most_stale_accounts)
app.add_periodic_task(45, queue_deletes)
app.add_periodic_task(90, refresh_account_with_oldest_post)
app.add_periodic_task(90, refresh_account_with_longest_time_since_refresh)

if __name__ == '__main__':
    app.worker_main()

