"""Payment configuration, tier pricing, and one-time secret key system.

Key format: LISA-T{1|2|3}-{XXXX}-{XXXX}-{XXXX}
- Tier-specific: T2 key cannot unlock T1 or T3
- 1-hour expiry from generation time
- Single-use (burned immediately on redemption)
- Bound to the Telegram ID of the purchaser
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

# ============================================================================
# TIER PRICING (NGN base; payment layer converts to local currency)
# ============================================================================

TIER_PRICES_NGN: dict[str, int] = {
    "tier1": 14_999,
    "tier2": 29_999,
    "tier3": 49_888,
}

# Approximate conversion rates (production: fetch live rates)
_NGN_TO_USD = 0.00067       # 1 NGN ≈ 0.00067 USD
_NGN_TO_USDT = 0.00067      # 1 NGN ≈ 0.00067 USDT
_NGN_TO_SOL = 0.0000042     # 1 NGN ≈ 0.0000042 SOL (approximate)
_NGN_TO_TON = 0.000015      # 1 NGN ≈ 0.000015 TON (approximate)

# Supported crypto networks
CRYPTO_WALLETS: dict[str, dict] = {
    "USDT-TRC20": {
        "address": "TKWFRpQgZRJxhHarM6etoQ2KSeWVK7QXBs",
        "network": "TRC20 (Tron)",
        "min_confirmations": 1,
    },
    "SOL": {
        "address": "FKky47wNt2viC1mvKurTqhSAdUe6GZeVcuCJHw1ZKxNn",
        "network": "Solana",
        "min_confirmations": 1,
    },
    "TON": {
        "address": "UQCJ9UyJzyAwWZhdulzB8K0veAqkjBhaP6v2exhyYWwTmar8",
        "network": "TON",
        "min_confirmations": 1,
    },
}

# Bank transfer details (commented out until virtual account is set up)
# BANK_TRANSFER_DETAILS = {
#     "bank_name": "Access Bank",
#     "account_name": "Xpredict",
#     "account_number": "XXXXXXXXXX",
#     "reference_prefix": "LISA",
# }

# Key expiry in seconds (1 hour)
KEY_EXPIRY_SECONDS = 3600

# Secret for signing keys (load from env or use a persistent secret)
_KEY_SECRET = os.environ.get("LISA_KEY_SECRET", "lisa-payment-secret-change-in-production")


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass(frozen=True)
class TierPrice:
    tier: str                    # "tier1", "tier2", "tier3"
    ngn: int                     # Price in NGN
    usd: float                   # Equivalent in USD

    usdt_trc20: float            # Equivalent in USDT (TRC20)
    sol: float                   # Equivalent in SOL
    ton: float                   # Equivalent in TON

    def format(self) -> str:
        return (
            f"💠 <b>{self.tier.upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🇳🇬 NGN: <code>₦{self.ngn:,}</code>\n"
            f"🇺🇸 USD: <code>${self.usd:.2f}</code>\n"

            f"₮ USDT (TRC20): <code>{self.usdt_trc20:.2f}</code>\n"
            f"◎ SOL: <code>{self.sol:.4f}</code>\n"
            f"💎 TON: <code>{self.ton:.4f}</code>"
        )


@dataclass(frozen=True)
class PaymentKey:
    key: str                     # "LISA-T1-XXXX-XXXX-XXXX"
    tier: str                    # "tier1", "tier2", "tier3"
    telegram_id: str              # Telegram user ID bound to this key
    created_at: float            # Unix timestamp
    expires_at: float            # created_at + KEY_EXPIRY_SECONDS
    used: bool = False           # Whether this key has been redeemed
    redeemed_by: Optional[str] = None  # Web user ID who redeemed it
    redeemed_at: Optional[float] = None

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        return now > self.expires_at

    def is_valid(self, now: Optional[float] = None) -> bool:
        return not self.used and not self.is_expired(now)


# ============================================================================
# PRICING
# ============================================================================

def get_tier_price(tier: str) -> TierPrice:
    """Get the price for a tier with currency conversions."""
    tier = tier.lower().replace("tier", "tier")
    if tier not in TIER_PRICES_NGN:
        raise ValueError(f"Unknown tier: {tier}. Valid: {list(TIER_PRICES_NGN.keys())}")
    ngn = TIER_PRICES_NGN[tier]
    return TierPrice(
        tier=tier,
        ngn=ngn,
        usd=round(ngn * _NGN_TO_USD, 2),
        usdt_trc20=round(ngn * _NGN_TO_USDT, 2),
        sol=round(ngn * _NGN_TO_SOL, 4),
        ton=round(ngn * _NGN_TO_TON, 4),
    )


def get_all_prices() -> list[TierPrice]:
    """Get all tier prices."""
    return [get_tier_price(t) for t in ("tier1", "tier2", "tier3")]


# ============================================================================
# KEY GENERATION & VERIFICATION
# ============================================================================

def _generate_segment(length: int = 4) -> str:
    """Generate a random alphanumeric segment (no ambiguous chars)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # No I, O, 0, 1
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_payment_key(tier: str, telegram_id: str) -> PaymentKey:
    """Generate a unique one-time payment key for a tier purchase.

    The key format is: LISA-T{1|2|3}-{XXXX}-{XXXX}-{XXXX}
    Example: LISA-T1-X7K9-PL2M-4RTB
    """
    tier_num = tier.lower().replace("tier", "")
    key = f"LISA-T{tier_num}-{_generate_segment()}-{_generate_segment()}-{_generate_segment()}"
    now = time.time()
    return PaymentKey(
        key=key,
        tier=tier.lower(),
        telegram_id=str(telegram_id),
        created_at=now,
        expires_at=now + KEY_EXPIRY_SECONDS,
    )


def verify_key(key: str, tier: str, telegram_id: str) -> tuple[bool, str]:
    """Verify a payment key is valid for redemption.

    Returns (is_valid, reason).
    """
    # Check format
    if not key or not key.startswith("LISA-T"):
        return False, "Invalid key format"

    # Check tier match
    key_tier = f"tier{key[6]}" if len(key) > 6 else ""
    if key_tier != tier.lower():
        return False, f"Key is for {key_tier.upper()}, not {tier.upper()}"

    # Check expiry (would need to look up key in storage in production)
    # This is a format-level check; actual expiry check happens against stored keys

    # Check telegram ID binding (would need to look up key in storage in production)
    # This ensures the person redeeming is the same person who purchased

    return True, "Valid"


def sign_key(key: str, telegram_id: str) -> str:
    """Create an HMAC signature for a payment key."""
    message = f"{key}:{telegram_id}".encode()
    return hmac.new(_KEY_SECRET.encode(), message, hashlib.sha256).hexdigest()[:16]


def verify_key_signature(key: str, telegram_id: str, signature: str) -> bool:
    """Verify the HMAC signature of a payment key."""
    expected = sign_key(key, telegram_id)
    return hmac.compare_digest(expected, signature)


# ============================================================================
# PAYMENT METHOD HELPERS
# ============================================================================

def get_payment_methods_markup() -> list[list[dict]]:
    """Get inline keyboard markup for payment method selection."""
    return [
        [{"text": "💲 USDT (TRC20)", "callback_data": "pay:usdt_trc20"}],
        [{"text": "◎ SOL", "callback_data": "pay:sol"}],
        [{"text": "💎 TON (Gram)", "callback_data": "pay:ton"}],
        # [{"text": "💳 Card (Paystack)", "callback_data": "pay:card"}],  # Future
        # [{"text": "🏦 Local Transfer", "callback_data": "pay:bank"}],    # Future: uncomment when virtual account is ready
    ]


def get_crypto_address(method: str) -> tuple[str, str]:
    """Get the wallet address and network name for a crypto method."""
    if method == "usdt_trc20":
        return CRYPTO_WALLETS["USDT-TRC20"]["address"], "TRC20 (Tron)"
    if method == "sol":
        return CRYPTO_WALLETS["SOL"]["address"], "Solana"
    if method == "ton":
        return CRYPTO_WALLETS["TON"]["address"], "TON"
    raise ValueError(f"Unknown crypto method: {method}")


# ============================================================================
# KEY STORAGE (SQLite-backed, in-memory for now)
# ============================================================================

class KeyStore:
    """Simple in-memory key store. Production: use SQLite/Redis."""

    def __init__(self) -> None:
        self._keys: dict[str, PaymentKey] = {}

    def store(self, key: PaymentKey) -> None:
        self._keys[key.key] = key

    def get(self, key_str: str) -> Optional[PaymentKey]:
        return self._keys.get(key_str)

    def mark_used(self, key_str: str, redeemed_by: str) -> bool:
        key = self._keys.get(key_str)
        if not key or key.used:
            return False
        self._keys[key_str] = PaymentKey(
            key=key.key,
            tier=key.tier,
            telegram_id=key.telegram_id,
            created_at=key.created_at,
            expires_at=key.expires_at,
            used=True,
            redeemed_by=redeemed_by,
            redeemed_at=time.time(),
        )
        return True

    def cleanup_expired(self) -> int:
        """Remove expired keys. Returns count removed."""
        now = time.time()
        expired = [k for k, v in self._keys.items() if v.is_expired(now)]
        for k in expired:
            del self._keys[k]
        return len(expired)


# Global key store instance
key_store = KeyStore()