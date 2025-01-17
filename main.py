"""Google Cloud Functions for Kernel CI reporting"""

import os
import time
import atexit
import tempfile
import base64
import json
import datetime
import logging
import smtplib
from urllib.parse import unquote
import jsonschema
import functions_framework
import google.cloud.logging
import kcidb

# Name of the Google Cloud project we're deployed in
PROJECT_ID = os.environ["GCP_PROJECT"]

# Setup Google Cloud logging, unless testing
if PROJECT_ID != "TEST_PROJECT":
    google.cloud.logging.Client().setup_logging()
    # Give logging some time to avoid occasional deployment failures
    time.sleep(5)
# Setup KCIDB-specific logging
kcidb.misc.logging_setup(
    kcidb.misc.LOGGING_LEVEL_MAP[os.environ.get("KCIDB_LOG_LEVEL", "NONE")]
)
# Get the module's logger
LOGGER = logging.getLogger()

# The dictionary of specs for databases and subscriber objects for the
# corresponding submission queue
_LOAD_QUEUE_SUBSCRIBERS = {}
# Maximum number of messages loaded from the submission queue in one go
LOAD_QUEUE_MSG_MAX = int(os.environ["KCIDB_LOAD_QUEUE_MSG_MAX"])
# Maximum number of objects loaded from the submission queue in one go
LOAD_QUEUE_OBJ_MAX = int(os.environ["KCIDB_LOAD_QUEUE_OBJ_MAX"])
# Maximum time for pulling maximum amount of submissions from the queue
LOAD_QUEUE_TIMEOUT_SEC = float(os.environ["KCIDB_LOAD_QUEUE_TIMEOUT_SEC"])

# The specification for the operational database (a part of DATABASE spec)
OPERATIONAL_DATABASE = os.environ["KCIDB_OPERATIONAL_DATABASE"]

# The specification for the archive database (a part of DATABASE spec)
ARCHIVE_DATABASE = os.environ["KCIDB_ARCHIVE_DATABASE"]

# The specification for the archive sample database (a part of DATABASE spec)
SAMPLE_DATABASE = os.environ["KCIDB_SAMPLE_DATABASE"]

# The specification for the database submissions should be loaded into
DATABASE = os.environ["KCIDB_DATABASE"]

# Minimum time between loading submissions into the database
DATABASE_LOAD_PERIOD = datetime.timedelta(
    seconds=int(os.environ["KCIDB_DATABASE_LOAD_PERIOD_SEC"])
)

# A whitespace-separated list of subscription names to limit notifying to
SELECTED_SUBSCRIPTIONS = \
    os.environ.get("KCIDB_SELECTED_SUBSCRIPTIONS", "").split()

# The address of the SMTP host to send notifications through
SMTP_HOST = os.environ["KCIDB_SMTP_HOST"]
# The port of the SMTP server to send notifications through
SMTP_PORT = int(os.environ["KCIDB_SMTP_PORT"])
# The username to authenticate to SMTP server with
SMTP_USER = os.environ["KCIDB_SMTP_USER"]
# The SMTP user's password
_SMTP_PASSWORD = None
# The address to tell the SMTP server to send the message to,
# overriding any recipients in the message itself.
SMTP_TO_ADDRS = os.environ.get("KCIDB_SMTP_TO_ADDRS", None)
# The name of the PubSub topic to post email messages to, instead of sending
# them to the SMTP server with SMTP_* parameters specified above. If
# specified, those parameters are ignored.
SMTP_TOPIC = os.environ.get("KCIDB_SMTP_TOPIC", None)
# The publisher object for email messages, if we're requested to send them to
# a PubSub topic
_SMTP_PUBLISHER = None
# The name of the subscription for email messages posted to the SMTP topic
SMTP_SUBSCRIPTION = os.environ.get("KCIDB_SMTP_SUBSCRIPTION", None)
# The address to use as the "From" address in sent notifications
SMTP_FROM_ADDR = os.environ.get("KCIDB_SMTP_FROM_ADDR", None)
# A string to be added to the CC header of notifications being sent out
EXTRA_CC = os.environ.get("KCIDB_EXTRA_CC", None)
# The dictionary of database specs and client instances
_DB_CLIENTS = {}
# The dictionary of database specs and object-oriented client instances
_OO_CLIENTS = {}
# KCIDB cache client instance
_CACHE_CLIENT = None
# The notification spool client
_SPOOL_CLIENT = None
# The updated URLs topic publisher
_UPDATED_URLS_PUBLISHER = None
# True if the database updates should be published to the updated queue
UPDATED_PUBLISH = bool(os.environ.get("KCIDB_UPDATED_PUBLISH", ""))
# The publisher object for the queue with patterns matching objects updated by
# loading submissions.
_UPDATED_QUEUE_PUBLISHER = None
# KCIDB cache storage bucket name
CACHE_BUCKET_NAME = os.environ.get("KCIDB_CACHE_BUCKET_NAME")


def get_smtp_publisher():
    """
    Get the created/cached publisher object for email messages, if we're
    requested to send them to a PubSub topic, or None, if not.
    """
    # It's alright, pylint: disable=global-statement
    global _SMTP_PUBLISHER
    if SMTP_TOPIC is not None and _SMTP_PUBLISHER is None:
        _SMTP_PUBLISHER = kcidb.mq.EmailPublisher(PROJECT_ID, SMTP_TOPIC)
    return _SMTP_PUBLISHER


def get_smtp_password():
    """Get the (cached) password for the SMTP user"""
    # It's alright, pylint: disable=global-statement
    global _SMTP_PASSWORD
    if _SMTP_PASSWORD is None:
        secret = os.environ["KCIDB_SMTP_PASSWORD_SECRET"]
        _SMTP_PASSWORD = kcidb.misc.get_secret(PROJECT_ID, secret)
    return _SMTP_PASSWORD


def get_load_queue_subscriber(database):
    """
    Create or get the cached subscriber object for the submission queue.

    Args:
        database:   The specification for the database the data from the queue
                    will be loaded into. Used to retrieve the accepted I/O
                    schema version.
    """
    # It's alright, pylint: disable=global-statement
    if database not in _LOAD_QUEUE_SUBSCRIBERS:
        _LOAD_QUEUE_SUBSCRIBERS[database] = kcidb.mq.IOSubscriber(
            PROJECT_ID,
            os.environ["KCIDB_LOAD_QUEUE_TOPIC"],
            os.environ["KCIDB_LOAD_QUEUE_SUBSCRIPTION"],
            schema=get_db_client(database).get_schema()[1]
        )
    return _LOAD_QUEUE_SUBSCRIBERS[database]


def get_updated_queue_publisher():
    """
    Create or get the cached publisher object for the queue with patterns
    matching objects updated by loaded submissions. Return None if update
    publishing is disabled.
    """
    # It's alright, pylint: disable=global-statement
    global _UPDATED_QUEUE_PUBLISHER
    if UPDATED_PUBLISH and _UPDATED_QUEUE_PUBLISHER is None:
        _UPDATED_QUEUE_PUBLISHER = kcidb.mq.ORMPatternPublisher(
            PROJECT_ID,
            os.environ["KCIDB_UPDATED_QUEUE_TOPIC"]
        )
    return _UPDATED_QUEUE_PUBLISHER


def get_db_credentials():
    """Fetch the database credentials, if needed and present"""
    if "PGPASSFILE" in os.environ:
        return
    secret_id = os.environ.get("KCIDB_PGPASS_SECRET")
    if secret_id is None:
        return

    pgpass = kcidb.misc.get_secret(PROJECT_ID, secret_id)
    (pgpass_fd, pgpass_filename) = tempfile.mkstemp(suffix=".pgpass")
    with os.fdopen(pgpass_fd, mode="w", encoding="utf-8") as pgpass_file:
        pgpass_file.write(pgpass)
    os.environ["PGPASSFILE"] = pgpass_filename
    atexit.register(os.remove, pgpass_filename)


def get_db_client(database):
    """
    Create or retrieve the cached database client.

    Args:
        database:   The specification for the database the client should
                    connect to.
    """
    if database not in _DB_CLIENTS:
        # Get the credentials
        get_db_credentials()
        # Create the client
        _DB_CLIENTS[database] = kcidb.db.Client(database)
    return _DB_CLIENTS[database]


def get_oo_client(database):
    """
    Create or retrieve the cached OO database client.

    Args:
        database:   The specification for the database the client should
                    connect to.
    """
    if database not in _OO_CLIENTS:
        _OO_CLIENTS[database] = kcidb.oo.Client(get_db_client(database))
    return _OO_CLIENTS[database]


def get_spool_client():
    """Create or retrieve the cached notification spool client."""
    # It's alright, pylint: disable=global-statement
    global _SPOOL_CLIENT
    if _SPOOL_CLIENT is None:
        collection_path = os.environ["KCIDB_SPOOL_COLLECTION_PATH"]
        _SPOOL_CLIENT = kcidb.monitor.spool.Client(collection_path)
    return _SPOOL_CLIENT


def get_updated_urls_publisher():
    """Create or retrieve the updated URLs publisher client"""
    # It's alright, pylint: disable=global-statement
    global _UPDATED_URLS_PUBLISHER
    if _UPDATED_URLS_PUBLISHER is None:
        _UPDATED_URLS_PUBLISHER = kcidb.mq.URLListPublisher(
            PROJECT_ID,
            os.environ["KCIDB_UPDATED_URLS_TOPIC"]
        )
    return _UPDATED_URLS_PUBLISHER


# This dictionary defines a structured specification
# for extracting URLs from I/O data.
URL_FIELDS_SPEC = {
    'checkouts': [
        {
            'patchset_files': [{'url': True}],
            'log_url': True
        }
    ],
    'builds': [
        {
            'input_files': [{'url': True}],
            'output_files': [{'url': True}],
            'config_url': True,
            'log_url': True
        }
    ],
    'tests': [
        {
            'output_files': [{'url': True}],
            'log_url': True
        }
    ]
}


def extract_fields(spec, data):
    """
    Extract specified fields from input data based
    on a specification structure.

    Args:
        spec: The specification for the fields to be extracted. Can be a
              boolean value, a dictionary containing field specifications, or
              a list containing a single field specification.

        data: The input data containing the fields to be extracted.

    Yields:
        URLs extracted based on the specifications.
    """
    if spec is True:
        yield data
    if isinstance(spec, dict) and isinstance(data, dict):
        for obj_type, obj_spec in spec.items():
            if obj_type in data:
                yield from extract_fields(obj_spec, data[obj_type])
    elif isinstance(spec, list) and isinstance(data, list):
        assert len(spec) == 1
        obj_specs = spec[0]
        for obj in data:
            yield from extract_fields(obj_specs, obj)


# pylint: disable=unused-argument
# As we don't use/need Cloud Function args
def kcidb_load_queue(event, context):
    """
    Load multiple KCIDB data messages from the load queue into the database,
    if it stayed unmodified for at least DATABASE_LOAD_PERIOD.
    """
    # pylint: disable=too-many-locals
    subscriber = get_load_queue_subscriber(DATABASE)
    db_client = get_db_client(DATABASE)
    io_schema = db_client.get_schema()[1]
    publisher = get_updated_queue_publisher()
    # Do nothing, if updated recently
    now = datetime.datetime.now(datetime.timezone.utc)
    last_modified = db_client.get_last_modified()
    LOGGER.debug("Now: %s, Last modified: %s", now, last_modified)
    if last_modified and now - last_modified < DATABASE_LOAD_PERIOD:
        LOGGER.info("Database too fresh, exiting")
        return

    # Pull messages
    msgs = subscriber.pull(
        max_num=LOAD_QUEUE_MSG_MAX,
        timeout=LOAD_QUEUE_TIMEOUT_SEC,
        max_obj=LOAD_QUEUE_OBJ_MAX
    )
    if msgs:
        LOGGER.info("Pulled %u messages", len(msgs))
    else:
        LOGGER.info("Pulled nothing, exiting")
        return

    # Create merged data referencing the pulled pieces
    LOGGER.debug("Merging %u messages...", len(msgs))
    data = io_schema.merge(
        io_schema.new(),
        (msg[1] for msg in msgs),
        copy_target=False, copy_sources=False
    )
    LOGGER.info("Merged %u messages", len(msgs))
    # Load the merged data into the database
    obj_num = io_schema.count(data)
    LOGGER.debug("Loading %u objects...", obj_num)
    db_client.load(data)
    LOGGER.info("Loaded %u objects", obj_num)

    # Get or create the URL publisher client
    urls_publisher = get_updated_urls_publisher()

    # Extract URLs from the data using URL_FIELDS_SPEC
    urls = extract_fields(URL_FIELDS_SPEC, data)

    # Divide the extracted URLs into slices of 64 using isliced
    urls_slices = kcidb.misc.isliced(set(urls), 64)

    # Process each slice of URLs
    for urls in urls_slices:
        LOGGER.info("Publishing extracted URLs: %s", list(urls))
        # Publish the extracted URLs
        urls_publisher.publish(list(urls))

    # Acknowledge all the loaded messages
    for msg in msgs:
        subscriber.ack(msg[0])
    LOGGER.debug("ACK'ed %u messages", len(msgs))

    if publisher:
        # Upgrade the data to the latest I/O version to enable ID extraction
        data = kcidb.io.SCHEMA.upgrade(data, copy=False)

        # Generate patterns matching all affected objects
        pattern_set = set()
        for pattern in kcidb.orm.query.Pattern.from_io(data):
            # TODO Avoid formatting and parsing
            pattern_set |= \
                kcidb.orm.query.Pattern.parse(repr(pattern) + "<*#")

        # Publish patterns matching all affected objects, if any
        if pattern_set:
            publisher.publish(pattern_set)
            LOGGER.info("Published updates made by %u loaded objects",
                        obj_num)


def kcidb_spool_notifications(event, context):
    """
    Spool notifications about objects matching patterns arriving from a Pub
    Sub subscription
    """
    oo_client = get_oo_client(DATABASE)
    spool_client = get_spool_client()
    # Reset the ORM cache
    oo_client.reset_cache()
    # Get arriving data
    pattern_set = set()
    for line in base64.b64decode(event["data"]).decode().splitlines():
        pattern_set |= kcidb.orm.query.Pattern.parse(line)
    LOGGER.info("RECEIVED %u PATTERNS", len(pattern_set))
    LOGGER.debug(
        "PATTERNS:\n%s",
        "".join(repr(p) + "\n" for p in pattern_set)
    )
    # Spool notifications from subscriptions
    for notification in kcidb.monitor.match(oo_client.query(pattern_set)):
        if not SELECTED_SUBSCRIPTIONS or \
           notification.subscription in SELECTED_SUBSCRIPTIONS:
            LOGGER.info("POSTING %s", notification.id)
            spool_client.post(notification)
        else:
            LOGGER.info("DROPPING %s", notification.id)


def kcidb_send_notification(data, context):
    """
    Send notifications from the spool
    """
    spool_client = get_spool_client()
    # Get the notification ID
    notification_id = context.resource.split("/")[-1]
    # Pick the notification if we can
    message = spool_client.pick(notification_id)
    if not message:
        return
    LOGGER.info("SENDING %s", notification_id)
    # send message via email
    send_message(message)
    # Acknowledge notification as sent
    spool_client.ack(notification_id)


def kcidb_pick_notifications(data, context):
    """
    Pick abandoned notifications and send them.
    """
    spool_client = get_spool_client()
    for notification_id in spool_client.unpicked():
        # Pick abandoned notification and resend
        message = spool_client.pick(notification_id)
        if not message:
            continue
        LOGGER.info("SENDING %s", notification_id)
        # send message via email
        send_message(message)
        # Acknowledge notification as sent
        spool_client.ack(notification_id)


def kcidb_purge_db(event, context):
    """
    Purge data from the operational database, older than the optional delta
    from the current (or specified) database timestamp, rounded to smallest
    delta component. Require that either the delta or the timestamp are
    present.
    """
    # Accepted databases and their specs
    databases = dict(op=OPERATIONAL_DATABASE, sm=SAMPLE_DATABASE)

    # Describe the expected event data
    schema = dict(
        type="object",
        properties=dict(
            database=dict(type="string", enum=list(databases)),
            timedelta=kcidb.misc.TIMEDELTA_JSON_SCHEMA,
        )
    )

    # Parse the input JSON
    string = base64.b64decode(event["data"]).decode()
    data = json.loads(string)
    jsonschema.validate(
        instance=data, schema=schema,
        format_checker=jsonschema.Draft7Validator.FORMAT_CHECKER
    )

    # Get the database client
    client = get_db_client(databases[data["database"]])

    # Parse/calculate the cut-off timestamp
    stamp = kcidb.misc.timedelta_json_parse(data["timedelta"],
                                            client.get_current_time())

    # Purge the data
    client.purge(stamp)


def send_message(message):
    """
    Send message via email.

    Args:
        message:    The message to send.
    """
    # Set From address, if specified
    if SMTP_FROM_ADDR:
        message['From'] = SMTP_FROM_ADDR
    # Add extra CC, if specified
    if EXTRA_CC:
        cc_addrs = message["CC"]
        if cc_addrs:
            message.replace_header("CC", cc_addrs + ", " + EXTRA_CC)
        else:
            message["CC"] = EXTRA_CC
    # If we're requested to divert messages to a PubSub topic
    publisher = get_smtp_publisher()
    if publisher is not None:
        publisher.publish(message)
    else:
        # Connect to the SMTP server
        smtp = smtplib.SMTP(host=SMTP_HOST, port=SMTP_PORT)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(SMTP_USER, get_smtp_password())
        try:
            # Send message
            smtp.send_message(message, to_addrs=SMTP_TO_ADDRS)
        finally:
            # Disconnect from the SMTP server
            smtp.quit()


def get_cache_client():
    """Create the cache client."""
    # It's alright, pylint: disable=global-statement
    global _CACHE_CLIENT
    if _CACHE_CLIENT is None:
        _CACHE_CLIENT = kcidb.cache.Client(
            CACHE_BUCKET_NAME, 5 * 1024 * 1024
        )
    return _CACHE_CLIENT


def kcidb_cache_urls(event, context):
    """
    Cloud Function triggered by Pub/Sub to cache URLs.

    Args:
        event (dict): The dictionary with the Pub/Sub event data.
            - data (str): contains newline-separated URLs.
        context (google.cloud.functions.Context): The event context.

    Returns:
        None
    """
    # Extract the Pub/Sub message data
    pubsub_message = base64.b64decode(event['data']).decode()

    # Get or create the cache client
    cache = get_cache_client()

    # Cache each URL
    for url in pubsub_message.splitlines():
        cache.store(url)


# The expiration time (a timedelta) of the URLs returned by the cache
# redirect, or None to return permanent URLs pointing to the public bucket.
CACHE_REDIRECT_TTL = datetime.timedelta(seconds=10)


@functions_framework.http
def kcidb_cache_redirect(request):
    """
    Handle the cache redirection for incoming HTTP GET requests.

    This function takes an HTTP request and processes it for
    cache redirection. If the request is a GET request,
    it extracts the URL from the request, checks if the URL exists
    in the cache, and performs a redirect if necessary.

    Args:
        request (object): The HTTP request object.

    Returns:
        tuple: A tuple containing the response body, status code,
        and headers epresenting the redirect response.
    """
    if request.method == 'GET':
        url_to_fetch = unquote(request.query_string.decode("ascii"))
        LOGGER.debug("URL %r", url_to_fetch)

        if not url_to_fetch:
            # If the URL is empty, return a 400 (Bad Request) error
            response_body = "Provide a valid URL to query from " \
                "the caching system."
            return (response_body, 400, {})

        # Check if the URL is in the cache
        cache_client = get_cache_client()
        cache = cache_client.map(url_to_fetch, ttl=CACHE_REDIRECT_TTL)
        if cache:
            LOGGER.info("Redirecting to the cache at %r", cache)
            # Redirect to the cached URL if it exists
            return ("", 302, {"Location": cache})

        # If the URL is not in the cache or not provided,
        # redirect to the original URL
        LOGGER.info("Redirecting to the origin at %r", url_to_fetch)
        return ("", 302, {"Location": url_to_fetch})

    # If the request method is not GET, return 405 (Method Not Allowed) error
    response_body = "Method not allowed."
    return (response_body, 405, {'Allow': 'GET'})
