"""
Web Push sending.

Kept as its own file rather than folded into db.py or app.py: db.py's
own header says it's "the only file that talks to SQLite directly" -
adding outbound HTTP calls to a push service there would break that.
app.py stays focused on routes. This file is the only one that talks to
the push service.

Needs a VAPID keypair (see generate_vapid_keys.py, a one-time setup
script) and the `pywebpush` package (add to requirements.txt).
"""

import json
import logging

from pywebpush import webpush, WebPushException

from config import VAPID_PRIVATE_KEY, VAPID_CLAIM_EMAIL
import db

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)


def _send_one(subscription, payload_json):
    endpoint = subscription.get("endpoint", "")
    p256dh = subscription.get("p256dh", "")
    auth = subscription.get("auth", "")

    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {
                    "p256dh": p256dh,
                    "auth": auth,
                },
            },
            data=payload_json,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{VAPID_CLAIM_EMAIL}"},
            # WNS (Microsoft's push service, used by Edge/Windows
            # subscribers - endpoints under notify.windows.com) REQUIRES
            # this header on every request, matched to the ttl we're
            # sending. pywebpush itself never sets it (confirmed against
            # the installed version - this is a known, still-unfixed gap
            # in the library, see web-push-libs/pywebpush#162), so every
            # push to a WNS endpoint gets silently rejected without it.
            # We always send the default ttl=0, so this must be
            # "no-cache" - "cache" is only correct if ttl is ever made
            # nonzero. Chrome/Firefox/etc. simply ignore this header, so
            # it's safe to always include.
            headers={"x-wns-cache-policy": "no-cache"},
        )
        logger.info("Successfully delivered push to endpoint: ...%s", endpoint[-20:])
        return True

    except WebPushException as ex:
        res = getattr(ex, "response", None)
        status = getattr(res, "status_code", "UNKNOWN")
        
        # Extract body safely regardless of stream state
        body = ""
        if res is not None:
            try:
                body = res.text
            except Exception:
                body = str(getattr(res, "content", ""))

        # WNS in particular returns a 400 with an EMPTY body and puts the
        # actual reason in response HEADERS instead (e.g.
        # X-WNS-ERROR-DESCRIPTION, X-WNS-STATUS) - logging only the body,
        # as before, meant these errors were undiagnosable from the logs.
        response_headers = dict(res.headers) if res is not None else {}

        logger.error(
            "\n================ [PUSH ERROR DEBUG] ================\n"
            "Status Code : %s\n"
            "Endpoint    : %s\n"
            "P256DH Key  : %s\n"
            "Auth Key    : %s\n"
            "VAPID Claim : mailto:%s\n"
            "Response    : %s\n"
            "Resp Headers: %s\n"
            "Exception   : %s\n"
            "====================================================",
            status,
            endpoint,
            p256dh[:15] + "..." if p256dh else "MISSING",
            auth[:10] + "..." if auth else "MISSING",
            VAPID_CLAIM_EMAIL,
            body if body.strip() else "[EMPTY RESPONSE BODY FROM PUSH SERVICE]",
            response_headers if response_headers else "[NO HEADERS CAPTURED]",
            repr(ex)
        )

        if status in (404, 410):
            logger.info("Pruning expired endpoint: ...%s", endpoint[-20:])
            db.remove_push_subscription(endpoint)

        return False


def send_to_all(title, body, url="/"):
    """Sends the same notification to every currently-subscribed device.
    Best-effort: one dead/failing subscription never blocks the rest."""
    subscriptions = db.get_all_push_subscriptions()
    if not subscriptions:
        return

    payload_json = json.dumps({"title": title, "body": body, "url": url})

    for sub in subscriptions:
        _send_one(sub, payload_json)


def notify_new_activity(events):
    """Turns a batch of freshly-logged activity_log rows into ONE push
    notification. Deliberately never sends one push per row - a bulk
    Excel upload can log 50+ price changes in one go, and 50 separate
    notification-tray entries would be far worse UX than the in-app
    activity feed it's meant to complement. A single event still gets a
    specific, deep-linkable notification; more than one collapses into a
    short summary that opens the catalog."""
    if not events:
        return

    if len(events) == 1:
        ev = events[0]
        title = ev["product_name"] or "Catalog update"
        body = ev["details"] or "Tap to view"
        slug = (ev["product_id"] or "").replace(" ", "")
        url = f"/product/{slug}" if slug else "/"
    else:
        title = f"{len(events)} catalog updates"
        body = "New prices and products are in - tap to see what changed."
        url = "/"

    send_to_all(title, body, url)
