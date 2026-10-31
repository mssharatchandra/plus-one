"""Deterministic, offline expense and curiosity helpers for the Plus One PoC.

Expense grammar (case-insensitive commands/currency, case-preserving names)::

    [I ]spent CURRENCY AMOUNT on DESCRIPTION with NAME [and NAME ...]
    [I ]paid AMOUNT [CURRENCY] for DESCRIPTION [split] with NAME [, NAME ...]
    NAME paid CURRENCY AMOUNT for DESCRIPTION with me and NAME
    expense CURRENCY AMOUNT for DESCRIPTION with NAME [ & NAME ...]

``on`` and ``for`` are interchangeable. Currency must be explicit unless the
caller supplies a previously confirmed profile default. Symbols are never
inferred. The user and payer are included exactly once in an equal split.
Names are human-readable labels, not resolved contacts. An extra minor unit
is allocated in shown order, starting with ``me``. Unequal shares, percentages,
and repayments are intentionally not parsed. Helpers never record expenses,
transfer money, or contact anybody.

Learning steps are small, deterministic storytelling exercises. Index is
zero-based and persisted by the caller. Later rounds revisit the same skills
with a new example; the caller persists reflections and displays continuity.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re
import unicodedata


CURRENCY_PRECISION = {
    "AED": 2, "AUD": 2, "CAD": 2, "CHF": 2, "EUR": 2, "GBP": 2,
    "INR": 2, "JPY": 0, "KWD": 3, "NZD": 2, "SGD": 2, "USD": 2,
}
_MAX_MAJOR = Decimal("100000000")
_EXAMPLE = "Try: spent INR 1200 on dinner with Maya and Rahul."
_AMOUNT = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
_HEADER = re.compile(
    r"^(?:(?P<self_command>(?:i\s+)?(?:spent|paid)|expense)|(?P<payer>.+?)\s+paid)\s+(?P<details>.+)$",
    re.IGNORECASE,
)
_DETAILS = re.compile(
    rf"^(?:(?P<currency_first>[a-z]{{3}})\s*(?P<amount_second>{_AMOUNT})|"
    rf"(?P<amount_first>{_AMOUNT})(?:\s*(?P<currency_second>[a-z]{{3}}))?)"
    rf"\s+(?:on|for)\s+(?P<description>.+?)\s+(?:split\s+)?with\s+(?P<names>.+)$",
    re.IGNORECASE,
)
_NAME_SEPARATOR = re.compile(r"\s*(?:,\s*(?:and\s+)?|\s+and\s+|\s*&\s*)\s*", re.IGNORECASE)
_RESERVED_NAMES = {
    "me", "myself", "i", "you", "yourself", "we", "us", "everyone", "all",
    "each", "equally", "split", "paid", "owes", "owe", "shares", "share",
    "percent", "percentage", "except", "excluding", "including", "only",
    "payer", "by", "and", "with", "plus", "minus", "at", "for", "on",
}


def _currency(currency: str) -> str:
    if not isinstance(currency, str) or currency.upper() not in CURRENCY_PRECISION:
        raise ValueError("Please use a supported currency code: " + ", ".join(CURRENCY_PRECISION) + ".")
    return currency.upper()


def format_money(amount_minor: int, currency: str) -> str:
    """Format signed integer minor units without a float conversion."""
    currency = _currency(currency)
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ValueError("Money must be stored as integer minor units.")
    precision = CURRENCY_PRECISION[currency]
    sign = "-" if amount_minor < 0 else ""
    units, fraction = divmod(abs(amount_minor), 10 ** precision)
    number = f"{sign}{units}"
    if precision:
        number += f".{fraction:0{precision}d}"
    return f"{currency} {number}"


def _parse_amount(raw: str, currency: str) -> int:
    try:
        amount = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        raise ValueError("Please give one positive decimal amount.") from None
    if not amount.is_finite() or amount <= 0 or amount > _MAX_MAJOR:
        raise ValueError("The amount must be greater than zero and at most 100000000.")
    # Decimal arithmetic obeys the ambient precision. Expand it so even a
    # long fractional input cannot round to an apparently valid minor unit.
    with localcontext() as context:
        context.prec = max(28, len(raw) + CURRENCY_PRECISION[currency] + 5)
        scaled = amount * (10 ** CURRENCY_PRECISION[currency])
    if scaled != scaled.to_integral_value():
        precision = CURRENCY_PRECISION[currency]
        raise ValueError(f"{currency} supports {precision} decimal places. Please correct the amount; I won't round it silently.")
    return int(scaled)


def _parse_names(raw: str, *, allow_me: bool = False) -> list[str]:
    # A single trailing full stop is normal sentence punctuation, not a name.
    raw = raw.removesuffix(".").strip()
    names = _NAME_SEPARATOR.split(raw)
    if not 1 <= len(names) <= 20:
        raise ValueError("Please name between 1 and 20 friends for this equal split.")
    normalized = set()
    result = []
    for raw_name in names:
        name = " ".join(unicodedata.normalize("NFC", raw_name).split())
        if not name or len(name) > 60 or len(name.split()) > 4:
            raise ValueError("Please give each friend's name, separated by commas or 'and'.")
        words = re.split(r"[\s'’\-]+", name.casefold())
        if any(word in _RESERVED_NAMES for word in words) and not (allow_me and name.casefold() == "me"):
            raise ValueError("Please list only participant names; use 'me' for yourself. This demo supports equal shares.")
        if not all(char.isalpha() or char in " '-’" for char in name) or not any(char.isalpha() for char in name):
            raise ValueError("Please use names only, not amounts or split instructions, after 'with'. This demo supports equal shares.")
        key = name.casefold()
        if key in normalized:
            raise ValueError("Two friend names are the same. Please use distinct labels (for example, Maya Singh and Maya Patel).")
        normalized.add(key)
        result.append("me" if name.casefold() == "me" else name)
    return result


def parse_expense(text: str, default_currency: str | None = None) -> dict:
    """Parse a proposed equal expense split, never a recorded expense.

    ``default_currency`` must be a previously confirmed profile preference;
    absent that preference, missing currency always requires clarification.
    The user is included in the split and named payer is included exactly once.
    All assumptions and the user's actual ledger effect appear in the preview.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 1000:
        raise ValueError("Please give a short expense with an amount, currency and friends. " + _EXAMPLE)
    if any(unicodedata.category(char).startswith("C") for char in text):
        raise ValueError("Please send the expense as one line. " + _EXAMPLE)
    header = _HEADER.fullmatch(text.strip())
    match = _DETAILS.fullmatch(header.group("details")) if header else None
    if not match:
        raise ValueError("I need an amount, description and named friends, with an explicit currency or confirmed default. " + _EXAMPLE)
    raw_currency = match.group("currency_first") or match.group("currency_second")
    if not raw_currency and default_currency is None:
        raise ValueError("Which currency? Please include an explicit code such as INR or USD, or confirm a profile default. " + _EXAMPLE)
    currency = _currency(raw_currency or default_currency)
    amount_minor = _parse_amount(match.group("amount_first") or match.group("amount_second"), currency)
    description = match.group("description").strip()
    if not 1 <= len(description) <= 120 or not any(char.isalnum() for char in description):
        raise ValueError("Please give a description between 1 and 120 characters.")
    if re.search(r"\b(?:split|shares?|paid\s+by|excluding|except|percent|percentage|owed|owes)\b|%", description, re.IGNORECASE):
        raise ValueError("This demo supports equal shares. Put a named payer first, for example 'Maya paid INR 600 for dinner with me and Rahul'.")
    raw_payer = header.group("payer")
    payer = "me"
    if raw_payer:
        payer_names = _parse_names(raw_payer)
        if len(payer_names) != 1:
            raise ValueError("Please name one payer. Multiple payers are not supported in this demo.")
        payer = payer_names[0]
    listed = _parse_names(match.group("names"), allow_me=True)
    # Preserve the supplied friend labels and order, but include the user and
    # payer exactly once even when their names also occur in the participant list.
    names = ["me"]
    seen = {"me"}
    for name in ([payer] if payer != "me" else []) + listed:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            names.append(name)
    if len(names) < 2:
        raise ValueError("Please name at least one friend for this shared expense.")
    base, remainder = divmod(amount_minor, len(names))
    shares = [{"name": name, "amount_minor": base + (index < remainder)} for index, name in enumerate(names)]
    return {"description": description, "currency": currency, "amount_minor": amount_minor, "payer": payer, "shares": shares}


def format_expense(proposal: dict) -> str:
    """Render the exact proposed ledger effect and ask before recording it."""
    currency = proposal["currency"]
    total = format_money(proposal["amount_minor"], currency)
    lines = [f"Let's check this together: {proposal['description']} — {total}.",
             f"Paid by {'you' if proposal['payer'] == 'me' else proposal['payer']}; split equally between you and these friends:"]
    for share in proposal["shares"]:
        label = "You" if share["name"] == "me" else share["name"]
        lines.append(f"• {label}: {format_money(share['amount_minor'], currency)}")
    values = [share["amount_minor"] for share in proposal["shares"]]
    if max(values) != min(values):
        lines.append("Any leftover minor units go in the order shown, starting with you.")
    if proposal["payer"] == "me":
        debts = [f"{share['name']} would owe you {format_money(share['amount_minor'], currency)}"
                 for share in proposal["shares"] if share["name"] != "me"]
    else:
        own_share = next(share["amount_minor"] for share in proposal["shares"] if share["name"] == "me")
        debts = [f"You would owe {proposal['payer']} {format_money(own_share, currency)}"]
    lines.append("Proposed internal balance change: " + "; ".join(debts) + ".")
    lines.append("Nothing is recorded yet. Reply confirm to save, or cancel. This only updates your private local ledger; nobody is contacted and no money moves.")
    return "\n".join(lines)


_STORY_STEPS = (
    ("Find one real moment",
     "A small, specific moment is easier to picture than a broad summary. Start with a place, a person and something happening.",
     "Choose a real moment that fits your goal. Write where you were, who was there and what happened in one or two sentences.",
     "Which detail makes the moment easiest to picture?"),
    ("Give the story a want",
     "A story gains direction when someone wants something. The want can be ordinary: being heard, catching a train or finishing a difficult conversation.",
     "Use your previous moment, or choose a new one. Write: 'I wanted ___, but ___.' Keep the obstacle concrete.",
     "What did the person want, and why did it matter to them?"),
    ("Show the turning point",
     "The turning point is a choice, discovery or action that changes what happens next. It need not be dramatic to be meaningful.",
     "Write three short beats: before the change, the moment of change, and after it. Put a clear action in the middle beat.",
     "What changed, and which action made the difference?"),
    ("Trade a label for a detail",
     "Instead of naming an emotion, a precise action or sensory detail can let your listener infer it. Use details you truly remember; don't invent facts in a real account.",
     "Find one general phrase in your story, such as 'I was nervous.' Replace it with one true action, sound or visible detail.",
     "Does your new detail reveal something, or is it just decoration?"),
    ("Land the ending",
     "A clear ending shows what changed or what you now understand. A modest, honest insight is often stronger than a grand moral.",
     "Read your short story aloud. End with one sentence about what changed for you. Remove one sentence that distracts from that ending.",
     "What should your listener remember after the last sentence?"),
    ("Tell it to one person",
     "Different listeners need different context. Keep the core moment intact, then choose the setup and detail that help one specific listener understand it.",
     "Pick an imagined listener for your goal. Retell the story in five sentences: setting, want, obstacle, action and change. No need to send it to anybody.",
     "Which sentence would you keep if you had only thirty seconds?"),
)


def learning_step(topic: str, goal: str, minutes: int, index: int) -> dict:
    """Return a timed storytelling step; caller owns saved goals/reflections.

    Topics accepted: ``storytelling``, ``story telling``, ``stories``. Steps are
    six per round; a later round prompts a new example, rather than pretending
    that lessons or reflections were supplied by a live language model.
    """
    if not isinstance(topic, str) or " ".join(topic.casefold().split()) not in {"storytelling", "story telling", "stories"}:
        raise ValueError("This offline demo has a storytelling journey. Try: learn storytelling.")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 200 or any(unicodedata.category(char).startswith("C") for char in goal):
        raise ValueError("Please give a short, one-line learning goal (up to 200 characters).")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 30:
        raise ValueError("Choose a small learning window from 1 to 30 minutes.")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 100000:
        raise ValueError("The learning step index must be a nonnegative integer up to 100000.")
    slot = index % len(_STORY_STEPS)
    round_number = index // len(_STORY_STEPS) + 1
    title, lesson, exercise, reflection = _STORY_STEPS[slot]
    goal = " ".join(goal.split())
    tailored = f"For your goal — {goal} — here's a {minutes}-minute step. {lesson}"
    if round_number > 1:
        exercise = "Start a new example for this practice round. " + exercise
    if minutes <= 2:
        exercise += " Keep it tiny: one rough sentence is enough today."
    elif minutes <= 5:
        exercise += " Spend most of the time on a rough attempt, then reread it once."
    else:
        exercise += " Make a first attempt, read it aloud, then revise one thing that helps your listener."
    return {
        "title": f"Storytelling {slot + 1}/{len(_STORY_STEPS)} · round {round_number}: {title}",
        "lesson": tailored,
        "exercise": exercise,
        "reflection_prompt": reflection + " Reply with your reflection when you're ready; we can use it to continue next time.",
    }
