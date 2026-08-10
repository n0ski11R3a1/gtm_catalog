"""
One-time setup: generates the VAPID keypair Web Push needs to sign
notifications. Run this ONCE, then paste the output into config.py (or
your environment variables) and never regenerate it - rotating the keys
would silently invalidate every existing subscription, forcing every
device to re-subscribe.

Usage:
    python generate_vapid_keys.py
"""

from py_vapid import Vapid02
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import base64


def main():
    vapid = Vapid02()
    vapid.generate_keys()

    # Raw, URL-safe base64 forms - what pywebpush and the browser's
    # PushManager.subscribe() applicationServerKey both expect.
    private_raw = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = vapid.public_key.public_bytes(
        encoding=Encoding.X962,
        format=PublicFormat.UncompressedPoint,
    )

    def b64url(raw_bytes):
        return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")

    print("Add these to config.py (or your environment):\n")
    print(f'VAPID_PRIVATE_KEY = "{b64url(private_raw)}"')
    print(f'VAPID_PUBLIC_KEY = "{b64url(public_raw)}"')
    print('VAPID_CLAIM_EMAIL = "you@yourdomain.com"  # any contact email the push services can reach you at')


if __name__ == "__main__":
    main()
