"""
B2B usage-alert dispatch.

After each successful B2B translation we check whether the customer
has just crossed an alert threshold (80% or 100% of their included
monthly quota). Each threshold fires at most once per billing period
thanks to per-period flags on the usage_periods row.

Sending happens via the existing SMTP service; SMTP failures fail
soft so the translation response is never delayed or blocked.
"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import SubscriptionProduct, UsagePeriod
from app.models.user import User
from app.services.email import email_service
from app.services.subscription import get_current_usage_period

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertThreshold:
    """A usage-alert checkpoint expressed as a percentage of the included quota."""

    percent: int
    flag_attr: str  # boolean column on UsagePeriod that tracks "already fired"


# Ordered low → high so we evaluate (and email) ascending thresholds before
# the bigger ones in the same call.
THRESHOLDS: tuple[AlertThreshold, ...] = (
    AlertThreshold(percent=80, flag_attr="alert_80_sent"),
    AlertThreshold(percent=100, flag_attr="alert_100_sent"),
)


def _crossed_thresholds(
    period: UsagePeriod,
) -> list[AlertThreshold]:
    """
    Return the thresholds the user has reached this period but for which
    the alert flag is still unset. Empty list when nothing needs to fire.
    """
    if period.characters_limit <= 0:
        return []
    ratio_percent = (period.characters_used / period.characters_limit) * 100
    return [
        t
        for t in THRESHOLDS
        if ratio_percent >= t.percent and not getattr(period, t.flag_attr)
    ]


async def maybe_send_usage_alerts(
    db: AsyncSession,
    user: User,
    product: SubscriptionProduct,
) -> None:
    """
    Check the user's current usage period and fire any not-yet-fired
    usage-alert emails. Safe to call from a fire-and-forget asyncio
    task — never raises, always logs on failure.

    Currently only B2B subscribers get usage alerts; consumer customers
    see a similar progress bar in the web UI instead.
    """
    if product != SubscriptionProduct.B2B:
        return

    period = await get_current_usage_period(db, user.id)
    if period is None:
        return

    pending = _crossed_thresholds(period)
    if not pending:
        return

    for threshold in pending:
        try:
            sent = email_service.send_b2b_usage_alert_email(
                to_email=user.email,
                threshold=threshold.percent,
            )
        except Exception as e:  # noqa: BLE001 — never raise out of this background task
            logger.exception(
                "Usage-alert email send raised for user %s at %d%%: %s",
                user.id,
                threshold.percent,
                e,
            )
            continue

        if not sent:
            # SMTP not configured or transient failure — leave the flag
            # FALSE so we retry on the next translation.
            logger.warning(
                "Usage-alert email for user %s at %d%% returned False",
                user.id,
                threshold.percent,
            )
            continue

        # Mark the threshold as fired before committing so a duplicate
        # request racing alongside this one doesn't double-send.
        setattr(period, threshold.flag_attr, True)
        logger.info(
            "Sent B2B usage alert to user %s at %d%% threshold",
            user.id,
            threshold.percent,
        )

    await db.commit()
