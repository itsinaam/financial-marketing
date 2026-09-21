import argparse
import os
import sys

import requests
from dotenv import load_dotenv

X_TWEETS_URL = "https://api.x.com/2/tweets"


def create_post(access_token: str, text: str) -> dict:
    response = requests.post(
        X_TWEETS_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"text": text},
        timeout=30,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"X API returned HTTP {response.status_code}: {response.text}"
        )

    return response.json()


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Publish a test post to X.")
    parser.add_argument(
        "text",
        nargs="?",
        default="Test post from Financial Marketing",
        help="Text to publish on X.",
    )
    args = parser.parse_args()

    access_token = os.getenv("X_ACCESS_TOKEN")
    if not access_token:
        print("Missing X_ACCESS_TOKEN environment variable.", file=sys.stderr)
        return 1

    try:
        result = create_post(access_token, args.text)
    except (requests.RequestException, RuntimeError) as error:
        print(f"Post failed: {error}", file=sys.stderr)
        return 1

    post_id = result.get("data", {}).get("id")
    print(f"Post published successfully. Post ID: {post_id}")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
