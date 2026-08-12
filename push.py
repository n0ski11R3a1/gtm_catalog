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

import logging

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

        logger.error(
            "\n================ [PUSH ERROR DEBUG] ================\n"
            "Status Code : %s\n"
            "Endpoint    : %s\n"
            "P256DH Key  : %s\n"
            "Auth Key    : %s\n"
            "VAPID Claim : mailto:%s\n"
            "Response    : %s\n"
            "Exception   : %s\n"
            "====================================================",
            status,
            endpoint,
            p256dh[:15] + "..." if p256dh else "MISSING",
            auth[:10] + "..." if auth else "MISSING",
            VAPID_CLAIM_EMAIL,
            body if body.strip() else "[EMPTY RESPONSE BODY FROM PUSH SERVICE]",
            repr(ex)
        )

        if status in (404, 410):
            logger.info("Pruning expired endpoint: ...%s", endpoint[-20:])
            db.remove_push_subscription(endpoint)

        return False


def _send_one_original(subscription, payload_json):
    """Sends to a single subscription. Returns True on success, False on
    any failure. A 404/410 from the push service means the browser has
    permanently revoked/expired that subscription (uninstalled, cleared
    site data, etc.) - not a "try again later" condition - so those get
    pruned from push_subscriptions on the spot rather than left to fail
    forever on every future notification."""
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {
                    "p256dh": subscription["p256dh"],
                    "auth": subscription["auth"],
                },
            },
            data=payload_json,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{VAPID_CLAIM_EMAIL}"},
        )
        return True
    except WebPushException as ex:
        status = getattr(ex.response, "status_code", None)
        if status in (404, 410):
            db.remove_push_subscription(subscription["endpoint"])
        else:
            # Network blip, 5xx from the push service, rate limiting,
            # etc. - not this device's fault, leave the subscription in
            # place and just skip it for this notification.
            logger.warning("Push send failed (status=%s): %s", status, ex)
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
