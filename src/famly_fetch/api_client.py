import hashlib
import http.client
import json
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from importlib_resources import files

#: Network failures that surface while the response body is being read, after
#: urlopen() has already returned successfully. Retrying the urlopen() call
#: alone never sees these, so any retry that is meant to cover a transfer has
#: to catch them around the read as well. IncompleteRead in particular is what
#: a CDN produces when it announces a Content-Length and then delivers fewer
#: bytes.
TRANSIENT_READ_ERRORS = (
    http.client.IncompleteRead,
    ConnectionResetError,
    TimeoutError,
)


def _backoff_delay(retry_after, attempt, base_delay, max_delay):
    """
    Seconds to wait before the next attempt.

    A usable Retry-After from the server wins, clamped to [0, max_delay].
    Otherwise back off exponentially: base_delay * 2^attempt, capped.
    """
    try:
        return max(0.0, min(float(retry_after), max_delay))
    except (TypeError, ValueError):
        return min(base_delay * 2**attempt, max_delay)


def _discard_partial(file_path):
    """
    Remove a half-written file so the next attempt starts clean.

    A failed transfer leaves whatever bytes did arrive on disk. Left in place
    those look like a real download to anything that checks for the file, so
    they are cleared before retrying and before giving up.
    """
    try:
        Path(file_path).unlink(missing_ok=True)
    except OSError:
        pass


def _retrying(
    operation,
    cleanup=None,
    attempts=5,
    base_delay=2.0,
    max_delay=60.0,
    what="Request",
):
    """
    Run `operation`, retrying transient network failures with backoff.

    Retries HTTP 429, HTTP 5xx, network-level URLError and TRANSIENT_READ_ERRORS,
    sleeping with exponential backoff (base_delay * 2^attempt, capped at
    max_delay) between tries. A Retry-After header, when the server sends one,
    overrides the computed delay. Any other HTTP error, anything that is not a
    network failure, and the final failed attempt raise as usual.

    `cleanup` runs after every failed attempt, retryable or not, so a caller
    that leaves a side effect behind (a half-written file) can reset it before
    the next try and before the error escapes.
    """
    for attempt in range(attempts):
        try:
            return operation()
        except urllib.error.HTTPError as e:
            if cleanup:
                cleanup()
            if e.code != 429 and e.code < 500:
                raise
            error = e
            retry_after = e.headers.get("Retry-After")
        except (urllib.error.URLError, *TRANSIENT_READ_ERRORS) as e:
            if cleanup:
                cleanup()
            error = e
            retry_after = None
        except BaseException:
            # Not retryable (a non-200 body, a bug, Ctrl-C). Still reset any
            # partial side effect before letting it propagate.
            if cleanup:
                cleanup()
            raise
        if attempt == attempts - 1:
            raise error
        delay = _backoff_delay(retry_after, attempt, base_delay, max_delay)
        print(f"{what} failed ({error}), retrying in {delay:.0f}s...")
        time.sleep(delay)


def urlopen_with_backoff(req, attempts=5, base_delay=2.0, max_delay=60.0):
    """
    urllib.request.urlopen with retries for throttling and transient failures.

    Note this covers only establishing the response. Reading the body can fail
    separately, and the caller does that after this function has returned, so
    those failures are outside the retry -- use read_body_with_backoff or
    download_to_file_with_backoff when the whole exchange needs covering.
    """
    return _retrying(
        lambda: urllib.request.urlopen(req),
        attempts=attempts,
        base_delay=base_delay,
        max_delay=max_delay,
    )


def read_body_with_backoff(req, attempts=5, base_delay=2.0, max_delay=60.0):
    """
    Fetch a URL and return (status, body decoded as utf-8).

    The body read sits inside the retry, so a response that is truncated or
    reset partway through is fetched again rather than raising.
    """

    def _once():
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8")

    return _retrying(
        _once, attempts=attempts, base_delay=base_delay, max_delay=max_delay
    )


def download_to_file_with_backoff(
    req, file_path, attempts=5, base_delay=2.0, max_delay=60.0
):
    """
    Stream a URL to disk, retrying the whole transfer on transient failures.

    The body read sits inside the retry, so a transfer that is reset or
    truncated partway through is retried instead of raising. Each failed
    attempt's partial file is removed first, so a retry never appends to a stub
    and a final failure does not leave one behind.

    A non-200 response, any other HTTP error, and the final failed attempt
    raise as usual.
    """

    def _once():
        with urllib.request.urlopen(req) as r:
            if r.status != 200:
                raise Exception(f"Broken! {r.read().decode('utf-8')}")
            with open(file_path, "wb") as f:
                shutil.copyfileobj(r, f)

    _retrying(
        _once,
        cleanup=lambda: _discard_partial(file_path),
        attempts=attempts,
        base_delay=base_delay,
        max_delay=max_delay,
        what="Download",
    )


def get_device_id() -> str:
    """
    Generates a consistent device identifier as an UUID string.
    This function retrieves the hardware address as a 48-bit positive integer using `uuid.getnode()`,
    converts it to a hexadecimal string, hashes it using MD5 to ensure privacy and consistency,
    and then formats the hash as a UUID string.
    Returns:
        str: A 128-bit UUID string representing the device identifier.
    """

    raw_id = hex(uuid.getnode())
    # Hash + convert to UUID format (ensures consistent 128-bit UUID string)
    return str(uuid.UUID(hashlib.md5(raw_id.encode()).hexdigest()))


class ApiClient:
    _access_token = None

    def __init__(
        self,
        base_url: str,
        user_agent: str | None = None,
        access_token: str | None = None,
    ):
        """
        Initialize the ApiClient.

        Args:
            user_agent (str): The user agent to use for requests.
            access_token (str): Optional access token to use directly.
        """
        self._user_agent: str | None = user_agent
        self._device_id = get_device_id()
        self._access_token = access_token
        self._base = base_url

    @property
    def access_token(self) -> str | None:
        """The access token in use, whether passed in or obtained by login()."""
        return self._access_token

    def login(self, email, password):
        """
        Authenticate with the Famly API and store the access token for future requests.

        Args:
            email (str): The user's email address.
            password (str): The user's password.

        Raises:
            Exception: If the server returns a non-200 HTTP status code.
        """

        login_data = self.make_graphql_request(
            "Authenticate",
            {
                "email": email,
                "password": password,
                "deviceId": self._device_id,
                "legacy": False,
            },
        )

        self._access_token = login_data["me"]["authenticateWithPassword"]["accessToken"]

    def get_child_notes(self, childId, cursor=None, first=10):
        data = self.make_graphql_request(
            "GetChildNotes",
            {
                "noteTypes": ["Classic"],
                "childId": childId,
                "parentVisible": True,
                "safeguardingConcern": False,
                "sensitive": False,
                "limit": first,
                "cursor": cursor,
            },
        )

        return data["childNotes"]

    def learning_journey_query(self, childId, cursor=None, first=10):
        data = self.make_graphql_request(
            "LearningJourneyQuery",
            {
                "childId": childId,
                "variants": [
                    "REGULAR_OBSERVATION",
                    "PARENT_OBSERVATION",
                ],
                "first": first,
                "next": cursor,
            },
        )

        return data["childDevelopment"]["observations"]

    def make_graphql_request(self, method, variables):
        query = files("famly_fetch.graphql").joinpath(f"{method}.graphql").read_text()

        postBody = {"operationName": method, "variables": variables, "query": query}

        data = self.make_api_request(
            "POST",
            f"/graphql?{method}",
            body=postBody,
        )

        return data["data"]

    def make_api_request(self, method, path, body=None, params=None):
        """
        Make a request to the Famly API and return the response.

        Args:
            method (str): The HTTP method to use for the request (e.g., "GET", "POST").
            path (str): The path of the API endpoint (e.g., "/graphql?Authenticate").
            body (dict, optional): The body of the request. Defaults to None.
            params (dict, optional): The query parameters to include in the request. Defaults to None.

        Returns:
            dict: The JSON response from the server.

        Raises:
            urllib.error.HTTPError: If the server couldn't fulfill the request.
            Exception: If the server returns a non-200 HTTP status code.
        """

        b = None
        if body:
            b = json.dumps(body).encode("utf-8")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._user_agent:
            headers["User-Agent"] = self._user_agent

        # If we already have the token, use it
        if self._access_token:
            headers["x-famly-accesstoken"] = self._access_token

        url = self._base + path

        if params:
            query_string = urllib.parse.urlencode(params)
            url += "?" + query_string

        req = urllib.request.Request(url=url, headers=headers, method=method, data=b)
        try:
            status, body = read_body_with_backoff(
                req, attempts=5, base_delay=2.0, max_delay=60.0
            )
            if status != 200:
                raise Exception(f"Broken! {body}")

            try:
                return json.loads(body)
            except Exception as _e:
                return body
        except urllib.error.HTTPError as e:
            # The server couldn't fulfill the request
            print("Error code: ", e.code)
            print("Response body: ", e.read())

    def feed(
        self,
        cursor: str | None = None,
        older_than: str | None = None,
        limit: int | None = None,
    ):
        params = {}
        if cursor:
            params["cursor"] = cursor
        if older_than:
            params["olderThan"] = older_than
        if limit:
            params["first"] = limit
        return self.make_api_request("GET", "/api/feed/feed/feed", params=params)

    def me_me_me(self):
        """
        Get information about the currently authenticated user.

        Returns:
            dict: The JSON response from the server.

        Raises:
            urllib.error.HTTPError: If the server couldn't fulfill the request.
            Exception: If the server returns a non-200 HTTP status code.
        """

        return self.make_api_request("GET", "/api/me/me/me")

    def get_relations(self, child_id: str) -> list[dict]:
        """
        Get the relations of a given child ID.

        Args:
            child_id (str): The ID of the child.

        Returns:
            list[dict]: A list of dictionary representing the relations.
        """
        return self.make_api_request(
            "GET", "/api/v2/relations", params={"childId": child_id}
        )
