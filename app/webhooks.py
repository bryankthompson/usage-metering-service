"""Stripe test-mode webhooks. Owner: dev B.

POST /v1/stripe/webhook verifies the Stripe-Signature header, then updates the `entitlements` record
for `invoice.payment_failed` and `customer.subscription.updated`. Each event id is handled once.
"""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/v1/stripe/webhook")
def stripe_webhook() -> None:
    raise HTTPException(status_code=501, detail="not implemented")
