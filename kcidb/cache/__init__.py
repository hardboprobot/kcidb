"""KCIDB URL caching system"""

import datetime
import hashlib
import logging
from email.header import Header
import urllib.parse
import requests
import google.auth
import google.auth.transport.requests
from google.cloud import storage

# Module's logger
LOGGER = logging.getLogger(__name__)


class Client:
    """KCIDB cache urls client."""
    def __init__(self, bucket_name, max_store_size):
        """
        Initialize a cache client.

        Args:
            bucket_name:    The name of the GCS bucket containing the cache.
            max_store_size: Maximum size the file can have to be stored.
        """
        self.bucket_name = bucket_name
        self.max_store_size = max_store_size
        self.client = storage.Client()

    @classmethod
    def _extract_content_disposition(cls, response):
        content_disposition = response.headers.get("Content-Disposition")

        if content_disposition is None:
            parsed_url = urllib.parse.urlparse(response.url)
            original_filename = parsed_url.path.rsplit('/', 1)[-1]
            if original_filename:
                encoded_filename = Header(original_filename, "utf-8").encode()
                content_disposition = \
                    f'attachment; filename="{encoded_filename}"'
            else:
                content_disposition = 'attachment'

        return content_disposition

    def store(self, url):
        """
        Attempt to store a URL in the cache. The URL contents is not
        downloaded if it's already in the cache or if it doesn't match the
        requirements (max_store_size).

        Args:
            url:    The URL to try to cache.
        """
        object_name = self._format_object_name(url)

        # Cache every 256th URL only for the trial period
        if not object_name.endswith("00"):
            return

        blob = self.client.bucket(self.bucket_name).blob(object_name)
        if blob.exists():
            LOGGER.debug("URL %r already exists, not caching.", url)
            return

        try:
            # Performing HEAD request first
            response = requests.head(url, timeout=10, allow_redirects=True)

            if response.status_code == 200:
                content_type = response.headers["Content-Type"]

                # Check the size of the content before downloading
                content_length = response.headers.get("Content-Length")
                if content_length is None:
                    LOGGER.warning("No Content-Length for %r, not caching.",
                                   url)
                    return

                content_length = int(content_length)
                if content_length > self.max_store_size:
                    LOGGER.warning("URL %r size (%d) exceeds "
                                   "max_store_size (%d), not caching.",
                                   url, content_length, self.max_store_size)
                    return

                # Perform the GET request to download the contents
                response = requests.get(url, timeout=10, allow_redirects=True)
                if response.status_code == 200:
                    contents = response.content

                    blob.content_disposition = \
                        self._extract_content_disposition(response)

                    blob.upload_from_string(
                        contents,
                        content_type=content_type
                    )
                    LOGGER.info("URL %r successfully cached.", url)
                    return

            LOGGER.warning("Failed to download URL %r. Status code: %d",
                           url, response.status_code)

        except requests.exceptions.RequestException as err:
            LOGGER.warning("Error downloading URL %r: %s", url, str(err))

    @classmethod
    def _format_object_name(cls, url):
        """
        Format a cache object name for a given (potentially) cached URL.
        Does not access the GCS storage.

        Args:
            url:    The (potentially) cached URL to format the object name for.

        Returns:
            The object name of the (potentially) cached URL.
        """
        # Generate a unique hash for the URL as the object name
        return hashlib.sha256(url.encode()).hexdigest()

    def _format_public_url(self, url):
        """
        Format a public URL for a given (potentially) cached URL.
        Does not access the GCS storage.

        Args:
            url:    The (potentially) cached URL to format the public URL for.

        Returns:
            The public URL of the (potentially) cached URL.
        """
        return (
            f"https://storage.googleapis.com/"
            f"{self.bucket_name}/{self._format_object_name(url)}"
        )

    def map(self, url, ttl=None):
        """
        Map a URL to the public URL of its cached contents, if it is cached.

        Args:
            url:    The potentially-cached URL to map.
            ttl:    A timedelta representing the expiration time of the
                    returned (signed) URL, or None to have a permanent URL
                    pointing to the cached URL (in a public bucket).

        Returns:
            The public URL of the cached content, if the URL is cached.
            None if the URL is not cached.
        """
        assert isinstance(url, str)
        assert ttl is None or isinstance(ttl, datetime.timedelta)

        object_name = self._format_object_name(url)
        blob = self.client.bucket(self.bucket_name).blob(object_name)
        if blob.exists():
            if ttl is None:
                return self._format_public_url(url)
            credentials = google.auth.default()[0]
            if credentials.token is None:
                credentials.refresh(google.auth.transport.requests.Request())
            return blob.generate_signed_url(
                version="v4", method="GET", expiration=ttl,
                service_account_email=credentials.service_account_email,
                access_token=credentials.token
            )
        return None

    def is_stored(self, url):
        """
        Check if a URL is stored in the cache or not.

        Args:
            url:    The URL to check.

        Returns:
            True if the URL is cached, False if not.
        """
        object_name = self._format_object_name(url)
        blob = self.client.bucket(self.bucket_name).blob(object_name)
        return blob.exists()

    def fetch(self, url):
        """
        Retrieve the contents of a URL if cached.

        Args:
            url:    The URL to retrieve the cached content of.

        Returns:
            The binary contents of the cached URL or None if not cached.
        """
        object_name = self._format_object_name(url)
        blob = self.client.bucket(self.bucket_name).blob(object_name)

        if blob.exists():
            return blob.download_as_bytes()
        return None

    def empty(self):
        """Empty the cache (remove all contents)."""
        bucket = self.client.bucket(self.bucket_name)
        for blob in bucket.list_blobs():
            blob.delete()
