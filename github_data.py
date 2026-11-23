import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from time import sleep, time
from urllib.parse import quote, urlencode


class DataError(RuntimeError):
    pass


class GitHubError(DataError):
    def __init__(self, message):
        super().__init__(message)
        self.not_found = "(HTTP 404)" in message


class RateLimitError(GitHubError):
    pass


def run_command(args, *, binary=False, **kwargs):
    return subprocess.run(
        args, encoding=None if binary else "utf-8", capture_output=True, timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs,
    )


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def endpoint(path, **params):
    return f"{path}?{urlencode(params)}" if params else path


def pages(fetch, first):
    seen = set()
    connection = first
    while True:
        yield from connection["nodes"]
        info = connection["pageInfo"]
        if not info["hasNextPage"]:
            return
        cursor = info["endCursor"]
        if not cursor or cursor in seen:
            raise DataError("GitHub pagination did not advance")
        seen.add(cursor)
        connection = fetch(cursor)


COMMENT = """
    id url body createdAt publishedAt path diffHunk
    author { login __typename } replyTo { id }
    originalCommit { oid } pullRequestReview { commit { oid } }
"""
PAGE = "pageInfo { hasNextPage endCursor }"
THREAD = f"id comments(first:8) {{ {PAGE} nodes {{ {COMMENT} }} }}"
PULL = (
    f"number mergedAt headRefOid mergeCommit {{ oid }} "
    f"reviewThreads(first:25) {{ {PAGE} nodes {{ {THREAD} }} }}"
)


class GitHub:
    def __init__(self, max_rate_wait=60):
        self.max_rate_wait = max_rate_wait

    def _call(self, method, path, payload=None):
        args = ["gh", "api", "--hostname", "github.com", "--method", method, "--include", path]
        if payload is not None:
            args.extend(["--input", "-"])
        for attempt in range(3):
            result = run_command(args, input=json.dumps(payload) if payload is not None else None)
            header, _, body = result.stdout.partition("\n\n")
            headers = dict(
                (key.lower(), value.strip()) for line in header.splitlines()[1:] if ":" in line
                for key, value in [line.split(":", 1)]
            )
            response = json.loads(body) if not result.returncode else None
            errors = response.get("errors", []) if method == "POST" and isinstance(response, dict) else []
            message = result.stderr.strip()
            limited = (
                "rate limit" in message.lower()
                or "HTTP 429" in message
                or any(error.get("type") == "RATE_LIMITED" for error in errors)
            )
            if limited:
                delay = max(1, int(headers.get("retry-after", "60")))
                if headers.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in headers:
                    delay = max(int(headers["x-ratelimit-reset"]) - int(time()) + 1, 1)
                if attempt == 2 or delay > self.max_rate_wait:
                    raise RateLimitError(f"GitHub rate limit reached. Retry in {delay}s; progress is saved.")
                sleep(delay)
                continue
            if not result.returncode:
                return response
            if attempt == 2 or not re.search(
                r"HTTP 5\d\d|REFUSED_STREAM|connection reset|timeout|unexpected EOF", message, re.I
            ):
                raise GitHubError(f"{path}: {message}")
            sleep(2 ** attempt)

    def get(self, path, **params):
        return self._call("GET", endpoint(path, **params))

    def query(self, query, **variables):
        result = self._call("POST", "graphql", {"query": query, "variables": variables})
        if result.get("errors"):
            raise GitHubError("; ".join(error["message"] for error in result["errors"]))
        return result["data"]

    def public(self, repo):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo, re.ASCII):
            raise DataError(f"Invalid repository: {repo}")
        info = self.get(f"repos/{repo}")
        if info["visibility"] != "public" or info["full_name"].lower() != repo.lower():
            raise DataError(f"Not a canonical public github.com repository: {repo}")

    def rest_pages(self, path, **params):
        for page in range(1, 1_000_000):
            items = self.get(path, **params, page=page, per_page=100)
            yield from items
            if len(items) < 100:
                return
        raise DataError(f"Pagination did not complete for {path}")

    def merged(self, repo, start, end):
        self.public(repo)
        return self._merged(repo, start, end)

    def _merged(self, repo, start, end):
        query = f"repo:{repo} is:pr is:merged merged:{start}..{end}"
        kwargs = dict(q=query, sort="created", order="asc", per_page=100)
        response = self.get("search/issues", **kwargs, page=1)
        total = response["total_count"]
        if total > 1000 or response["incomplete_results"]:
            if start == end:
                items = self.rest_pages(f"repos/{repo}/pulls", state="closed")
                return self._merged_dates(items, start, end, search=False)
            middle = start + (end - start) // 2
            left = self._merged(repo, start, middle)
            right = self._merged(repo, middle + timedelta(days=1), end)
            if left.keys() & right.keys():
                raise DataError(f"Overlapping search results for {repo}")
            return left | right
        items = response["items"]
        for page in range(2, (total + 99) // 100 + 1):
            response = self.get("search/issues", **kwargs, page=page)
            if response["incomplete_results"] or response["total_count"] != total:
                raise DataError(f"GitHub search changed while paging {repo}; rerun the collector")
            items.extend(response["items"])
        result = self._merged_dates(items, start, end, search=True)
        if len(result) != total or len(items) != total:
            raise DataError(f"GitHub search count did not match the UTC merge cohort for {repo}")
        return result

    @staticmethod
    def _merged_dates(items, start, end, *, search):
        result = {}
        for pr in items:
            value = pr["pull_request"]["merged_at"] if search else pr["merged_at"]
            if value and start <= instant(value).astimezone(timezone.utc).date() <= end:
                if pr["number"] in result:
                    raise DataError("GitHub returned a duplicate pull request")
                result[pr["number"]] = value
        return result

    def pulls(self, repo, expected):
        owner, name = repo.split("/")
        numbers = sorted(expected)
        for offset in range(0, len(numbers), 10):
            fields = " ".join(
                f"p{n}:pullRequest(number:{n}) {{ {PULL} }}" for n in numbers[offset:offset + 10]
            )
            result = self.query(
                "query($owner:String!, $name:String!) { repository(owner:$owner,name:$name) "
                f"{{ visibility {fields} }} }}", owner=owner, name=name,
            )["repository"]
            if not result or result.pop("visibility") != "PUBLIC":
                raise DataError(f"{repo} is not public")
            for n in numbers[offset:offset + 10]:
                node = result[f"p{n}"]
                if not node or not node["mergedAt"] or instant(node["mergedAt"]) != instant(expected[n]):
                    raise DataError(f"Merge timestamp mismatch for {repo}#{n}")
                yield self._pull(repo, owner, name, node)

    def _pull(self, repo, owner, name, pr):
        def more_threads(cursor):
            result = self.query(
                "query($owner:String!, $name:String!, $n:Int!, $cursor:String!) "
                "{ repository(owner:$owner,name:$name) { visibility pullRequest(number:$n) "
                f"{{ reviewThreads(first:25, after:$cursor) {{ {PAGE} nodes {{ {THREAD} }} }} }} }} }}",
                owner=owner, name=name, n=pr["number"], cursor=cursor,
            )["repository"]
            if not result or result["visibility"] != "PUBLIC":
                raise DataError(f"{repo} is not public")
            return result["pullRequest"]["reviewThreads"]

        merged = instant(pr["mergedAt"])
        threads, missing, seen = [], 0, set()
        for thread in pages(more_threads, pr["reviewThreads"]):
            if thread["id"] in seen:
                raise DataError(f"Duplicate review thread on {repo}#{pr['number']}")
            seen.add(thread["id"])
            first = thread["comments"]["nodes"]
            if not first or first[0]["replyTo"]:
                missing += 1
                continue
            root = first[0]
            if not root["publishedAt"] or instant(root["publishedAt"]) >= merged:
                continue

            def more_comments(cursor):
                return self.query(
                    "query($id:ID!, $cursor:String!) { node(id:$id) { ... on PullRequestReviewThread "
                    f"{{ comments(first:100, after:$cursor) {{ {PAGE} nodes {{ {COMMENT} }} }} }} }} }}",
                    id=thread["id"], cursor=cursor,
                )["node"]["comments"]

            comments = [
                {"url": comment["url"], "body": comment["body"], "at": comment["publishedAt"]}
                for comment in pages(more_comments, thread["comments"])
                if comment["replyTo"] and comment["publishedAt"]
                and instant(comment["publishedAt"]) < merged
            ]
            revision = (
                (root["originalCommit"] or {}).get("oid")
                or ((root["pullRequestReview"] or {}).get("commit") or {}).get("oid")
            )
            threads.append({
                "id": thread["id"], "url": root["url"], "opened": root["publishedAt"],
                "created": root["createdAt"], "path": root["path"], "hunk": root["diffHunk"],
                "body": root["body"], "author": root["author"], "reviewed": revision,
                "replies": comments,
            })
        threads.sort(key=lambda t: (instant(t["opened"]), instant(t["created"]), t["id"]))
        return {
            "repo": repo, "number": pr["number"], "merged": pr["mergedAt"],
            "merge": (pr["mergeCommit"] or {}).get("oid"), "head": pr["headRefOid"],
            "threads": threads, "missing_roots": missing,
        }


def blob_url(repo, revision, path):
    return f"https://github.com/{repo}/blob/{revision}/{quote(path)}"
