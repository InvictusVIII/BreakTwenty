"""Bundled category rulepack.

Rules ship in code (no per-user duplication in DB). User-defined rules live
in the `category_rules` table and win over bundled rules by default priority.

Matching semantics (see `bundled_rule_matches`):
- Default: case-insensitive substring match against `description`, with `|`
  as alternation (any alternative is enough).
- `is_regex=True` switches to `re.search(pattern, description, IGNORECASE)`.
- Optional filters narrow when a rule applies: `provider`, `account_type`,
  `amount_sign`, `transaction_type`.

Lower `priority` wins. Tiers:
  50  — provider-scoped bank-internal patterns (highest specificity)
  100 — provider-scoped but pattern-only
  200 — specific Canadian/major merchant names
  300 — brand families with mild ambiguity (Steam, Uber, etc.)
  500 — generic patterns (PHARMACY, DENTAL, etc.)
  700 — broad catch-alls

`category_seed_key` must match the slugified seed name of a leaf in
`app.services.categories.SEED_TAXONOMY` (e.g. "coffee", "online_shopping").
The rule applier resolves the seed_key to the user's current `category_id`
via the stable `categories.seed_key` column, so user renames of the leaf's
display name never break rule resolution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BundledRule:
    pattern: str
    category_seed_key: str
    priority: int = 500
    provider: str | None = None
    account_type: str | None = None
    amount_sign: str | None = None  # "positive" | "negative"
    transaction_type: str | None = None
    is_regex: bool = False
    label: str | None = None


# Bump when the bundled rulepack changes in a way that should re-apply to users'
# already-imported (non-manual) transactions. On app startup a version-gated
# one-time re-evaluation (`services/categories.ensure_categorization_ruleset_current`)
# re-resolves existing transactions when the stored per-user version differs, so
# code-shipped rule improvements reach historical data on update without a re-add.
# Manual tags are skipped and user rules still win, so it never clobbers a user's
# own categorizations. Keep it a simple monotonically-increasing integer.
RULESET_VERSION = 3


# ===========================================================================
# INCOME
# ===========================================================================

INCOME_RULES: list[BundledRule] = [
    # Provider-scoped payroll patterns observed in real data
    BundledRule("EFT Deposit from CANADA LIFE", "refund", priority=50, provider="tangerine", label="Canada Life — benefit/insurance refund (maintainer golden standard)"),
    BundledRule("EFT Deposit from ONTARIO HEALTH", "paycheck", priority=50, provider="tangerine", label="Ontario Health payroll"),
    BundledRule("EFT Deposit from CANADA", "reimburse", priority=55, provider="tangerine", label="Generic 'from CANADA …' deposit — maintainer tags Reimburse (CANADA LIFE rule above wins for that payer)"),
    BundledRule("Direct deposit from", "paycheck", priority=60, label="Direct deposit (generic)"),
    BundledRule("PAYROLL|PAYE|PAY DEP|PAY DEPOSIT|DEP PAIE|REGULAR PAYROLL DEP|PAYROLL DEP", "paycheck", amount_sign="positive", priority=100, label="Payroll keywords (inflows only — avoids 'PAYE' matching 'PAYER' on outgoing CRA payments)"),

    # Refunds (positive amounts only — usually merchant credits)
    BundledRule("CREDIT FOR FRAUDULENT CHARGE", "refund", amount_sign="positive", priority=50, provider="amex"),
    BundledRule("CREDIT FOR PURCHASE|CREDIT FOR RETURN|MERCHANT CREDIT|MERCHANDISE CREDIT|RETURN CREDIT", "refund", priority=200, amount_sign="positive"),
    BundledRule("REFUND", "refund", priority=300, amount_sign="positive"),
    BundledRule("Credit Card Rewards Redemption", "reimburse", priority=80, provider="tangerine", label="Tangerine cashback (maintainer tags Reimburse)"),
    BundledRule("CASHBACK|CASH BACK", "refund", priority=400, amount_sign="positive"),

    # Reimbursements
    BundledRule("REIMBURSEMENT|EXPENSE REIMBURSEMENT", "reimburse", priority=300, amount_sign="positive"),
]


# ===========================================================================
# INVESTMENTS
# ===========================================================================
# Investment kinds (buy/sell/dividend/etc.) are already handled by the
# type-mapping fallback. No bundled rules needed unless we ever want to
# override (e.g., split BUY rows into Stock / ETF / Crypto subcategories).
INVESTMENT_RULES: list[BundledRule] = []


# ===========================================================================
# TRANSFERS
# ===========================================================================

TRANSFER_RULES: list[BundledRule] = [
    # Credit card payments — provider-specific bank-internal patterns observed in DB
    BundledRule("PAYMENT RECEIVED - THANK YOU", "cc_payment", priority=50, provider="amex"),
    BundledRule("THANK YOU TAN GERINE", "cc_payment", priority=50, provider="scotiabank", label="Scotia CC payment from Tangerine"),
    BundledRule("Tangerine Credit Card Payment", "cc_payment", priority=50, provider="tangerine"),
    BundledRule("PAYMENT - THANK YOU", "cc_payment", priority=60, label="Generic CC payment-received"),
    BundledRule("Bill Payment - SCOTIABANK VISA", "cc_payment", priority=60, provider="tangerine"),
    BundledRule("Bill Payment.*VISA|Bill Payment.*MASTERCARD|Bill Payment.*AMEX|Bill Payment.*CREDIT CARD", "cc_payment", priority=100, is_regex=True),
    BundledRule("CREDIT CARD PAYMENT|CC PAYMENT|CARD PAYMENT", "cc_payment", priority=400),

    # Line-of-credit payment legs: a LOC never pays you — any inflow onto a LOC
    # account is debt paydown (a payment/credit reducing the balance). Route the
    # whole inflow side to Loan Payment so both legs of a LOC payment carry the
    # same label.
    BundledRule(".*", "loan_payment", account_type="loc", amount_sign="positive", priority=45, is_regex=True, label="LOC inflow = loan paydown"),
    # Explicit loan-advance / LoC-drawdown wording -> Loan Advance (neutral),
    # provider-agnostic so any "Loan Advance …" description matches directly.
    BundledRule("LOAN ADVANCE|CASH ADVANCE|LINE OF CREDIT ADVANCE|LOC ADVANCE", "loan_advance", priority=120, label="Loan / LoC advance"),
    # No blanket "LoC outflow = Loan Advance" fallback: outflows are categorized by
    # description (explicit "Loan Advance …" wording above; interest/fees by their
    # own rules) and anything unidentified falls through to Uncategorized rather than
    # a misleading Loan Advance default — important for manual imports, which carry no
    # transaction type and would otherwise bury interest/fees as advances.

    # Interac e-Transfers are person-to-person money flow (not internal account
    # transfers), so they count as income/expense by amount sign: money in =
    # E-Transfer Received (income), money out = E-Transfer Sent (expense). Purpose
    # is unknown, so they land in that catch-all pair for the user to refine; a
    # self-transfer between your own accounts is the rare case, fixed manually.
    # (Must NOT match the Interac fee rule below — fee rule has lower/higher-precedence priority.)
    BundledRule("INTERAC.{0,4}E.?TRANSFER|E.?TRANSFER SENT|E.?TRANSFER RECEIVED|VIREMENT INTERAC|INTERAC ETRNSFR RECVD|INTERAC ETRNSFR SENT", "etransfer_received", amount_sign="positive", priority=200, is_regex=True, label="Interac e-Transfer received"),
    BundledRule("INTERAC.{0,4}E.?TRANSFER|E.?TRANSFER SENT|E.?TRANSFER RECEIVED|VIREMENT INTERAC|INTERAC ETRNSFR RECVD|INTERAC ETRNSFR SENT", "etransfer_sent", amount_sign="negative", priority=200, is_regex=True, label="Interac e-Transfer sent"),

    # Tangerine LoC drawdowns (advances) read Loan Advance on BOTH legs — the
    # LoC-side leg ("Loan Advance to Tangerine Chequing …") and the cash-side
    # arrival ("Internet Deposit from Line of Credit …"). Higher precedence than
    # the generic Tangerine transfer rule below, so a draw reads as a debt
    # movement, symmetric with the Loan Payment legs. (The reverse leg, "Internet
    # Withdrawal to Line of Credit", is already Loan Payment at priority 80.)
    BundledRule("Loan Advance to|Internet Deposit from Line of Credit", "loan_advance", priority=90, provider="tangerine", label="Tangerine LoC drawdown (both legs)"),
    # Bank-internal transfers (Tangerine specifically uses these phrasings extensively)
    BundledRule("Payment from Tangerine Chequing|Internet Withdrawal to|Internet Deposit from", "transfer", priority=100, provider="tangerine"),
    # Tangerine ships debit-card store purchases with type=transfer, which would
    # otherwise silently bury an un-recognized purchase as a Transfer. Last
    # resort (priority 950, after every merchant rule) flags unmatched Tangerine
    # debit purchases as Uncategorized so they surface for the user's attention
    # instead of hiding in Transfer. Real transfers above use "Withdrawal to" /
    # "Deposit from" phrasings that never contain "Purchase".
    BundledRule("Interac - Purchase|Visa Debit - Purchase|Interac Purchase", "uncategorized", priority=950, provider="tangerine", label="Tangerine debit purchase, no merchant match -> flag for review"),
    BundledRule("Auto-withdrawal by Tangerine", "transfer", priority=100, provider="eqbank"),
    BundledRule("Transfer to Rome|Transfer from Rome|Transfer to RBC|Transfer from RBC|Direct deposit from Tangerine|Payment to QUESTRADE", "transfer", priority=100, provider="eqbank"),

    # RBC bank-internal
    BundledRule("Inter-FI Fund Tr|Online Banking transfer|BR TO BR", "transfer", priority=100, provider="rbc"),
    BundledRule("Investment Tangerine", "transfer", priority=80, provider="rbc", label="RBC inbound transfer from Tangerine investments"),

    # Wise
    BundledRule("Sent money to|Received money from|Topped up account|Converted .* to", "transfer", priority=100, provider="wise", is_regex=True),

    # Own-account funding / registered-account contributions — money moving into
    # your own investment/registered accounts (often the other leg lives in an
    # account BreakTwenty can't see), so these are transfers, not spend. Generic across
    # banks. Needed because the type-mapping fallback no longer guesses transfers.
    BundledRule("BILL PAYMENT - INTERACTIVE BROKERS|PAYMENT TO INTERACTIVE BROKERS|EFT DEPOSIT FROM INTERACTIVE BROKERS", "transfer", priority=120, label="Interactive Brokers funding/withdrawal"),
    BundledRule("FHSA CONTRIBUTION|RRSP CONTRIBUTION|TFSA CONTRIBUTION|RESP CONTRIBUTION|LIRA CONTRIBUTION|RRIF CONTRIBUTION", "transfer", priority=120, label="Registered-account contribution"),

    # IBKR
    BundledRule("DISBURSEMENT INITIATED|CASH RECEIPTS|ELECTRONIC FUND TRANSFER", "transfer", priority=100, provider="ibkr", label="IBKR cash movement"),

    # Coinbase — outbound crypto sends (to external self-custody wallets, or
    # gasless-send features). The connector type-maps these to Withdrawal by
    # default, but semantically they're wallet-to-wallet transfers of your own
    # asset rather than spending. Scoped to provider + transaction_type so it
    # can't accidentally catch fiat withdrawals from other providers. Matches
    # all descriptions on Coinbase outbound rows (description-less + descriptive
    # forms like `2023 gasless USDC send`).
    BundledRule(
        ".*", "transfer", priority=80, provider="coinbase",
        transaction_type="withdrawal", is_regex=True,
        label="Coinbase outbound crypto send",
    ),

    # Scotiabank — balance-transfer-style cash advances (debt shuffled from a
    # credit card to a Scotialine LOC). The LOC-side row is the "wrong side"
    # of a transfer between two of the user's debt accounts, so it belongs in
    # Transfer rather than the type-mapping fallback `Withdrawal`. Pattern
    # targets Scotia's actual wording (`CASH ADVANCE` paired with `AMEX` or
    # `CARTES AMEX`) so it can't accidentally catch a real ATM cash advance.
    BundledRule(
        "CASH ADVANCE.*AMEX|CARTES AMEX|BALANCE TRANSFER",
        "transfer", priority=100, provider="scotiabank", is_regex=True,
        label="Scotia balance-transfer cash advance",
    ),

    # Questrade brokerage funding — chequing → Questrade pre-funded account
    # is a transfer between the user's own accounts, not spending. Catches
    # both `Bill Payment - QUESTRADE INC - ******7813` (Tangerine bill-pay
    # form) and `Online Banking payment - 6188 QUESTRADE` (RBC form).
    BundledRule(
        "QUESTRADE INC|Bill Payment.*QUESTRADE|Online Banking payment.*QUESTRADE",
        "transfer", priority=80, is_regex=True, label="Questrade brokerage funding",
    ),

    # Generic
    BundledRule("WIRE TRANSFER|EFT TRANSFER|EFT WITHDRAWAL|EFT DEPOSIT|BANK TRANSFER|WIRE OUT|WIRE IN", "transfer", priority=500),
    BundledRule("TRANSFER TO|TRANSFER FROM|XFER TO|XFER FROM", "transfer", priority=600),
]


# ===========================================================================
# FINANCIAL (fees + interest paid)
# ===========================================================================

FINANCIAL_RULES: list[BundledRule] = [
    # Interest paid — provider/account-specific patterns
    BundledRule("Loan interest", "interest_paid", priority=50, provider="rbc", label="RBC loan interest"),
    BundledRule("INTEREST PAYMENT", "interest_paid", priority=60, provider="rbc", amount_sign="negative"),
    BundledRule("USD DEBIT INT FOR|CAD DEBIT INT FOR", "interest_paid", priority=50, provider="ibkr", label="IBKR debit interest"),
    # Direction-aware interest: gated to connector type=interest (so security
    # names like "…HIGH INTEREST SAVINGS" and balance transfers don't match),
    # then routed by amount sign — authoritative since bank wording is ambiguous
    # ("Interest Paid" can mean paid TO you; "Interest Billed" = charged to you).
    # Money out = charged (expense); money in = earned (income).
    BundledRule(".*", "interest_paid", priority=70, amount_sign="negative", transaction_type="interest", is_regex=True, label="Interest charged (outflow)"),
    BundledRule(".*", "interest", priority=70, amount_sign="positive", transaction_type="interest", is_regex=True, label="Interest earned (inflow)"),
    BundledRule("BALANCEPROTECTOR PREMIUM", "insurance", priority=80, provider="rbc", label="RBC LOC credit insurance"),
    BundledRule("INTEREST BILLED|INTEREST CHARGED|PURCHASE INTEREST|CASH ADVANCE INTEREST|FINANCE CHARGE|FINANCING FEE", "interest_paid", priority=200, label="Interest charged (description, type-agnostic — covers manual imports)"),

    # Bank fees
    BundledRule("MEMBERSHIP FEE INSTALLMENT", "bank_fees", priority=50, provider="amex", label="AMEX annual fee"),
    BundledRule("MONTHLY PLAN FEE|MONTHLY ACCOUNT FEE|MAINTENANCE FEE|MONTHLY FEE", "bank_fees", priority=200),
    BundledRule("OVERDRAFT FEE|NSF FEE|NSF CHARGE|RETURNED ITEM FEE|OVERLIMIT FEE", "bank_fees", priority=200),
    BundledRule("ANNUAL FEE", "bank_fees", priority=300),

    # ATM fees
    BundledRule("ATM FEE|ATM WITHDRAWAL FEE|ATM SURCHARGE|OUT.OF.NETWORK ATM|NON-CIBC ATM", "atm_fees", priority=200, is_regex=True),
    BundledRule("Fee Electronic transaction", "atm_fees", priority=80, provider="rbc"),

    # Transfer/transaction fees
    BundledRule("INTERAC e-Transfer fee|INTERAC.*FEE|E.?TRANSFER FEE", "fee", priority=100, is_regex=True, label="Interac transfer fee"),
    BundledRule("FX FEE|FOREIGN TRANSACTION FEE|FOREIGN CURRENCY CONVERSION", "fee", priority=200),
    BundledRule("WIRE FEE|EFT FEE|TRANSACTION FEE", "fee", priority=400),

    # Loan payments — personal lines of credit, non-mortgage/non-auto debt servicing
    BundledRule("Bill Payment - SCOTIABANK SCOTIALINE", "loan_payment", priority=60, label="Scotia line of credit"),
    BundledRule("SCOTIALINE|RBC RIGHT LINE|RBC ROYALLINE|TD LOC|CIBC PERSONAL LINE|BMO LINE OF CREDIT|BMO HOMEOWNER ROC|TANGERINE LOC", "loan_payment", priority=100, label="Major Canadian LOCs"),
    BundledRule("Internet Withdrawal to Line of Credit", "loan_payment", priority=80, provider="tangerine", label="Tangerine LOC repayment"),
    BundledRule("PERSONAL LOAN|LINE OF CREDIT PAYMENT|LOC PAYMENT", "loan_payment", priority=300, label="Generic LOC / personal loan"),
    BundledRule("LOAN PAYMENT", "loan_payment", priority=600, label="Generic loan payment catch-all"),
]


# ===========================================================================
# FOOD & DRINK
# ===========================================================================

FOOD_RULES: list[BundledRule] = [
    # Groceries — Canadian chains
    BundledRule(
        "LOBLAW|METRO  |METRO #|SOBEYS|FOOD BASICS|NO FRILLS|FRESHCO|FARM BOY|SAVE-ON-FOODS|SAVE ON FOODS|"
        "REAL CANADIAN SUPERSTORE|YOUR INDEPENDENT|FORTINOS|VALU.MART|VALU MART|LONGOS|"
        "BULK BARN|H MART|HMART|GALLERIA SUPERMARKET|T&T SUPERMARKET|TNT SUPERMARKET|"
        "WHOLE FOODS|TRADER JOE|LONGOS",
        "groceries", priority=200, label="Major Canadian grocers",
    ),
    BundledRule("COSTCO WHOLESALE|COSTCO\\b", "groceries", priority=300, is_regex=True, label="Costco — default to Groceries"),
    BundledRule("ADONIS|MAXI|SUPER C|IGA|PROVIGO|BONICHOIX|MARCHE|JEAN COUTU SUPER", "groceries", priority=200, label="Quebec grocers"),
    # Specialty / online-grocers Canadians order from. PAMPA DIRECT is a
    # Florida-billed Latin American specialty-foods online merchant.
    # STARSKY FINE FOODS is a Mississauga Eastern European grocer.
    BundledRule("PAMPA DIRECT|STARSKY FINE FOODS", "groceries", priority=200, label="Specialty / online grocers"),
    BundledRule("PRODUCE MARKET|GROCERY|GROCER|SUPERMARKET|FOOD MARKET|FOOD MART|BUTCHER|MEAT MARKET|FARMERS MARKET|GREENGROCER", "groceries", priority=700),

    # Coffee
    BundledRule(
        "TIM HORTONS|STARBUCKS|SECOND CUP|TIMOTHY|BALZACS|REUNION ISLAND|JJ BEAN|BLENZ|"
        "COFFEE TIME|COUNTRY STYLE|%ARABICA|ARABICA|ROOSTER COFFEE|HENDERSONS BREWING|"
        "TOWN CRIER|JIMMY.S COFFEE|SLOW COFFEE|JIMMEYS COFFEE|DAVIDS TEA|DAVID.S TEA|"
        "GREGG.S COFFEE|MCCAFE|AROMA ESPRESSO",
        "coffee", priority=200, label="Coffee shops",
    ),
    BundledRule("CAFE|COFFEE|ESPRESSO|ROASTERS|BREW BAR", "coffee", priority=600),

    # Fast Food (quick-service)
    # `\bA\s*&\s*W\b` catches both `A&W` (no-space) and `A & W #4869 ...`
    # (Canadian POS format with store number). PAPA JOHN covers Papa John's
    # in both `Papa John's` and `PAPA JOHNS` forms.
    BundledRule(
        "MCDONALD|BURGER KING|HARVEY|SUBWAY|WENDY|KFC|POPEYES|\\bA\\s*&\\s*W\\b|PAPA JOHN|"
        "TACO BELL|PANAGO|MARY BROWN|LITTLE CAESAR|MR\\.? SUB|MUCHO BURRITO|FIREHOUSE SUBS|"
        "BURRITO BANDIDOS|BURRITO BOYZ|BLAZE PIZZA|UNCLE TETSU|CHUNGCHUN RICE DOG|PITA LITE|BEAVERTAILS|"
        "DAIRY QUEEN|FIVE GUYS|SHAKE SHACK|CHIPOTLE|FRESHII|FRESH&CO|QUIZNOS|TIM HORTONS BREAKFAST",
        "fast_food", priority=200, is_regex=True, label="Quick-service chains",
    ),
    BundledRule("PIZZA PIZZA|PIZZA HUT|DOMINO.S PIZZA|PANAGO|BOSTON PIZZA|LITTLE CAESAR", "fast_food", priority=200, is_regex=True),

    # Restaurants (sit-down)
    BundledRule(
        "SWISS CHALET|KELSEY|MILESTONES|EARLS|JOEY|CACTUS CLUB|MOXIE|THE KEG|"
        "JACK ASTOR|MONTANAS|WHITESPOT|RED LOBSTER|OLIVE GARDEN|MILESTONES|TRATTORIA|"
        "OSTERIA|BISTRO|RAMEN|SUSHI|IZAKAYA|YAKITORI|PHO\\b|BANH MI|DIM SUM|HOTPOT|"
        "STEAK HOUSE|STEAKHOUSE|GRILL HOUSE|CHOPS|TIVA|TAVERNA",
        "restaurants", priority=200, is_regex=True, label="Sit-down restaurants",
    ),
    # Toronto / Canadian kitchen-pubs and food-first venues — per the
    # food-first rule, anywhere with a kitchen and a sit-down menu goes here
    # regardless of whether you also drank. Higher priority than the generic
    # Alcohol rule so brewery taprooms with food (Amsterdam Brewhouse, Bier
    # Markt, 3 Brewers, SP Toronto Brewing, Birrería Volo) don't get
    # swept into Alcohol by `BREWERY` / `BREWING`.
    BundledRule(
        # Toronto British/Irish/gastropubs
        "SCOTLAND YARD|SIN & REDEMPTION|SIN AND REDEMPTION|C,EST WHAT|"
        "DROM TABERNA|QUEEN & BEAVER|QUEEN AND BEAVER|"
        "STORM CROW MANOR|STORMCROWMANOR|SOLUNA|ENDS MEET PUB|"
        "THE ROSE AND CROWN|THE QUAIL AND FIRKIN|IMPERIAL PUB|BLACK DOG PUB|"
        "DUKE OF CORNWALL|DUKE OF KENT|GOODMAN PUB|BISHOP AND BELCHER|"
        "THE ARTFUL DODGER|HAIR OF THE DOG|THE CALEDONIAN|LUCKY CLOVER|"
        "THE GOLDEN PHEASANT|LIMERICK.S TRADITIONAL|=TOWNCRIER|TOWN CRIER|"
        # Brewery taprooms with food
        "BIRRERIA VOLO|TORONTO BREWING|AMSTERDAM BREWHOUSE|3 BREWERS|BIER MARKT|"
        "BEERHIVE|"
        # Toronto restaurants (specific cuisine spots)
        "TERRONI|BANGKOK GARDEN|PAPI CHULO|MADRAS MASALA|EATALY|"
        "RIO 40 DEGREES|RIO DEGREES|THE DISTILLERY RESTAUR|DNISTER|"
        "W BURGER BAR|LOBBY TORONTO|ARIA TORONTO|CONSTANTINE|CLUNY BISTRO|"
        "PIE BAR|QUE RICO TAPAS|AJISEN RAMEN|KINTON RAMEN|QUANTO BASTA|"
        "BAWARA|IMM THAI",
        "restaurants", priority=180, is_regex=True, label="Toronto kitchen-pubs + specific restaurants",
    ),
    BundledRule(
        # `RESTAURAN` (no trailing T) catches bank-truncated `…RESTAURAN` too.
        "RESTAURAN|BISTRO|GRILL|KITCHEN|EATERY|DINING|TRATTORIA|OSTERIA|TAVERN|"
        "PUB\\b|GASTROPUB|BREWHOUSE|BAR & GRILL|BAR AND GRILL|TAPAS|CANTINA|"
        "BRASSERIE|RISTORANTE|CANTEEN",
        "restaurants", priority=500, is_regex=True,
    ),

    # Alcohol — drinking-focused bars (cocktail bars, dive bars, vodka bars,
    # rooftops with minimal food). Distinct from kitchen-pubs above; if a
    # venue has a real kitchen and sit-down menu, it's already routed to
    # Restaurants at the higher-priority rule. `LCBO`/`BEER STORE`/wine
    # subscriptions stay here as retail alcohol.
    BundledRule(
        "LCBO|BEER STORE|SAQ\\b|BC LIQUOR|ALBERTA LIQUOR|NSLC|NLC\\b|"
        "WINE RACK|WINE SHOP|LIQUOR.*MART|LIQUOR STORE|LIQUOR MART|"
        "DRUMMOND BREWERY|"
        # Specific Toronto bars where food is a tiny menu / non-existent
        "THE AVIARY|PRAVDA VODKA|PRENUP BAR|RUBY SOHO|BAR \\+ MUSIC STUDIO|"
        "CASABLANCA LOUNGE|THE PORCH|GET LOUD|SNAKES & LAGERS|"
        "RESERVOIR LOUNGE|COMEDY BAR|CC LOUNGE|CIVIL LIBERTIES|"
        "TST-MIRACLE|MIDNIGHT MARKET|CNE EVENT BARS|SUGARBUSH VINEYARDS|"
        "SP MIELLERIE|"
        # Generic catch-all for breweries WITHOUT a kitchen-pub rule above
        "BREWERY",
        "alcohol", priority=200, is_regex=True, label="Liquor stores + drinking-focused bars",
    ),

    # Cannabis / psychedelics — Canadian dispensary chains + provincial cannabis boards
    BundledRule(
        "TOKYO SMOKE|CANNA CABANA|SPIRITLEAF|FIRE & FLOWER|FIRE AND FLOWER|"
        "VALUE BUDS|NOVA CANNABIS|ONE PLANT|HUNNY POT|MEDIPHARM|TWEED\\b|"
        "HEXO\\b|CANOPY GROWTH|APHRIA|TILRAY|AURORA CANNABIS|"
        "GREEN MERCHANT|FRIENDLY STRANGER",
        "cannabis", priority=180, is_regex=True, label="Canadian dispensary chains + producers",
    ),
    BundledRule(
        "ONTARIO CANNABIS|OCS\\b|OCS\\.CA|BC CANNABIS|ALBERTA CANNABIS|SQDC\\b|"
        "QUEBEC CANNABIS|SASK CANNABIS|NB CANNABIS|MB CANNABIS|NS CANNABIS|YUKON CANNABIS",
        "cannabis", priority=180, is_regex=True, label="Provincial cannabis boards",
    ),
    BundledRule(
        "CANNABIS|MARIJUANA|DISPENSARY|WEED\\b|MUSHROOM|PSILOCYBIN|MAGIC TRUFFLE",
        "cannabis", priority=400, is_regex=True, label="Generic cannabis / psychedelics catch-all",
    ),
    # Shisha / hookah lounges — per user preference, grouped under the
    # Cannabis seed_key (renamed to "Recreational" in their taxonomy) since
    # they're recreational social venues with tobacco/herbal smoke and
    # typically don't serve alcohol.
    BundledRule(
        "SHISHA LOUNGE|\\bSHISHA\\b|\\bHOOKAH\\b",
        "cannabis", priority=200, is_regex=True, label="Shisha / hookah lounges",
    ),

    # Delivery
    BundledRule("UBER\\s*EATS|UBEREATS|DOORDASH|SKIPTHEDISHES|SKIP\\s+THE\\s+DISHES|JUST EAT|FOODORA|GRUBHUB", "delivery", priority=200, is_regex=True),
    BundledRule("HELLOFRESH|CHEFSPLATE|GOODFOOD|FRESH PREP", "online_groceries", priority=200, label="Meal-kit subscriptions (Online Groceries per maintainer taxonomy)"),
    BundledRule("TOO GOOD TO GO", "online_groceries", priority=200, label="Too Good To Go (surplus-food pickup)"),

    # Treats — bubble tea, ice cream, frozen yogurt, donut shops, smoothies
    BundledRule(
        "CHATIME|COCO FRESH|GONG CHA|KUNG FU TEA|SHARETEA|PRESOTEA|BUBBLEOLOGY|"
        "TIGER SUGAR|XING FU TANG|MACHI MACHI|YIFANG|HEY TEA",
        "treats", priority=180, is_regex=True, label="Bubble tea chains",
    ),
    BundledRule(
        "BASKIN.ROBBINS|COLD STONE|MARBLE SLAB|DAIRY QUEEN|BEN.JERRY|HAAGEN.DAZS|"
        "MENCHIE|PINKBERRY|YOGEN FR|TUTTI FRUTTI|YOGURTY",
        "treats", priority=180, is_regex=True, label="Ice cream + frozen yogurt",
    ),
    BundledRule(
        "KRISPY KREME|DUNKIN|TIM HORTON.*DONUT|COUNTRY STYLE DONUTS|ROBIN.S DONUTS|"
        "BAKED GOODS|BAKERY",
        "treats", priority=200, is_regex=True, label="Donut + bakery treats",
    ),
    BundledRule(
        "BOOSTER JUICE|JAMBA JUICE|JUGO JUICE|SMOOTHIE KING|FRESHII\\s+SMOOTHIE|"
        "JUICE BAR|FRESH\\s+JUICE",
        "treats", priority=200, is_regex=True, label="Smoothie + juice bars",
    ),
    BundledRule("LINDT", "treats", priority=200, label="Lindt chocolatier"),
    BundledRule("BUBBLE TEA|BOBA\\b|FROZEN YOGURT|FROYO\\b|GELATO|CUPCAKE|MILKSHAKE", "treats", priority=400, is_regex=True, label="Generic dessert catch-all"),
]


# ===========================================================================
# HOUSING
# ===========================================================================

HOUSING_RULES: list[BundledRule] = [
    # Phone (mobile)
    BundledRule(
        "ROGERS\\b|BELL CANADA|BELL MOBILITY|BELL MOBILE|TELUS\\b|KOODO|FIDO\\b|"
        "FREEDOM MOBILE|FREEDOM\\b|VIRGIN PLUS|VIRGIN MOBILE|VIDEOTRON|PUBLIC MOBILE|"
        "CHATR|FIZZ|LUCKY MOBILE",
        "phone", priority=200, is_regex=True, label="Major Canadian mobile carriers",
    ),

    # Internet
    BundledRule(
        "TEKSAVVY|BEANFIELD|DIALLOG|EBOX\\b|VMEDIA|XPLORE|XPLORENET|ALTIMA|"
        "START\\.CA|DISTRIBUTEL|YOURTECH|CARRYTEL|ACANAC",
        "internet", priority=200, is_regex=True, label="Canadian ISPs",
    ),
    BundledRule("BELL FIBE|BELL INTERNET|ROGERS INTERNET|SHAW DIRECT|SHAW INTERNET|TELUS INTERNET", "internet", priority=180, label="Major ISP bundles"),

    # Utilities
    BundledRule(
        "HYDRO ONE|TORONTO HYDRO|HYDRO QUEBEC|HYDRO QC|BC HYDRO|"
        "ENBRIDGE|UNION GAS|FORTISBC|FORTIS BC|EPCOR|TORONTO WATER|"
        "ENERGYPLUS|DIRECT ENERGY|JUST ENERGY|GAS COMPANY",
        "utilities", priority=200, is_regex=True, label="Canadian utility providers",
    ),
    BundledRule("UTILITY|UTILITIES|ELECTRIC BILL|WATER BILL|GAS BILL", "utilities", priority=500),

    # Rent
    # Chexy is a Canadian rent-payment platform (pay rent by card). It also
    # processes the odd CRA payment as "CHEXY*CRA REVENUE …" — excluded here by
    # matching only the INC / RENT descriptors, so that one still routes to
    # Income Tax via the CRA rule.
    BundledRule("CHEXY INC|CHEXY RENT", "rent", priority=200, label="Chexy (rent-payment platform)"),
    BundledRule("RENT PAYMENT|MONTHLY RENT|RENT - |LANDLORD|PROPERTY MGMT|PROPERTY MANAGEMENT", "rent", priority=300),

    # Maintenance / cleaning
    BundledRule("HOME DEPOT|RONA\\b|LOWE.?S|HOME HARDWARE|CASTLE BUILDING|TIMBER MART", "maintenance", priority=200, is_regex=True),
    BundledRule("SPARKLE SOLUTIONS", "maintenance", priority=80, provider="tangerine", label="Cleaning service"),
    BundledRule("PLUMBING|ELECTRICIAN|HVAC|HEATING.*COOLING|HANDYMAN|CARPET CLEAN|JANITORIAL|MAID SERVICE", "maintenance", priority=400, is_regex=True),

    # Home insurance
    BundledRule("SQUARE ONE INSURANCE", "insurance", priority=80, label="SquareOne tenant/home insurance"),
    BundledRule("TENANT INSURANCE|HOME INSURANCE|RENTERS INSURANCE|CONTENTS INSURANCE|PROPERTY INSURANCE", "insurance", priority=300),

    # Mortgage payments
    BundledRule("MORTGAGE PAYMENT|MTGE PYMT|MTGE PMT|TD MTGE|MORTGAGE PYMT", "mortgage", priority=80, label="Canadian bank mortgage payment patterns"),
    BundledRule("Bill Payment.*MORTGAGE", "mortgage", priority=80, is_regex=True, label="Bill-pay to mortgage holder"),
    BundledRule("HELOC PAYMENT|HOME EQUITY LINE|HOME EQUITY LOC|HELOC PYMT", "mortgage", priority=100, label="HELOC payment (treated as mortgage)"),
    BundledRule("CHIP HOME EQUITY|CHIP REVERSE", "mortgage", priority=100, label="Reverse mortgage"),
    BundledRule("MORTGAGE\\b|MTGE\\b", "mortgage", priority=400, is_regex=True, label="Generic mortgage catch-all"),
]


# ===========================================================================
# TRANSPORTATION
# ===========================================================================

TRANSPORTATION_RULES: list[BundledRule] = [
    # Gas
    BundledRule(
        "ESSO|SHELL\\b|PETRO.?CAN|PETRO CANADA|HUSKY\\b|CIRCLE K|MOBIL\\b|CHEVRON|"
        "ULTRAMAR|COUCHE.?TARD|HASTY MARKET|FAS GAS|PIONEER GAS|CO.?OP GAS|"
        "CANADIAN TIRE GAS|7.ELEVEN GAS|CONVENIENCE GAS",
        "gas", priority=200, is_regex=True, label="Canadian gas stations",
    ),

    # Transit
    # Short three-letter transit acronyms (REM, RTM, STM, EXO, HSR, YRT, TTC)
    # need leading word boundaries — otherwise e.g. `REM\b` matches inside
    # broker descriptions like `HARVEST PREM YIELD TREASURY ETF` (PREM → REM).
    # BIKE SHARE TORONTO is grouped with Transit (not Fitness) since it's a
    # short-trip transportation alternative — same use as TTC for many users.
    BundledRule(
        "PRESTO\\b|PRESTO AUTL|\\bTTC\\b|GO TRANSIT|GO STATION|\\bCOMPASS\\b|TRANSLINK|"
        "YORK REGION TRANSIT|\\bYRT\\b|MIWAY|BRAMPTON TRANSIT|\\bHSR\\b|OC TRANSPO|"
        "VIA RAIL|\\bEXO\\b|\\bRTM\\b|\\bSTM\\b|\\bREM\\b|MTA\\*NYCT|MTA NYCT|MTA WMSC|"
        "BIKE SHARE TORONTO|CITY OF TORONTO FERRY",
        "transit", priority=200, is_regex=True, label="Public transit",
    ),

    # Ride share
    BundledRule("UBER\\s+TRIP|UBER\\s+CANADA|UBER\\s+\\*|^UBER\\s", "ride_share", priority=180, is_regex=True, label="Uber rides"),
    BundledRule("LYFT", "ride_share", priority=200),

    # Parking + tolls
    BundledRule("GREEN P|TORONTO PARKING|IMPARK|PRECISE PARKLINK|PARK PLUS|HONK MOBILE|PARKPASS|PAYBYPHONE", "parking", priority=200),
    BundledRule("PARKING|PARKADE|PARKING METER", "parking", priority=600),
    BundledRule("407 ETR|HWY 407|EXPRESS TOLL|TOLL ROAD|TOLL CHARGE", "tolls", priority=200),

    # Car
    BundledRule("BELAIR|BELAIRDIRECT|SONNET INSURANCE|TD INSURANCE AUTO|INTACT INSURANCE|AVIVA|CO.?OPERATORS|DESJARDINS INSURANCE|ECONOMICAL|WAWANESA|JOHNSON INSURANCE|TICO", "car_insurance", priority=300, is_regex=True),
    BundledRule("AUTO INSURANCE|CAR INSURANCE|VEHICLE INSURANCE", "car_insurance", priority=500),
    BundledRule("MIDAS|JIFFY LUBE|MR LUBE|MR\\.? LUBE|CANADIAN TIRE AUTO|MISTER LUBE|GOODYEAR|FOUNTAIN TIRE|KAL TIRE|FORMULA HONDA|AUTO SERVICE|MECHANIC|TIRE\\s|MUFFLER", "car_repair", priority=200, is_regex=True),

    # Car loans — dealer-financing arms + bank auto-loan products
    BundledRule(
        "GM FINANCIAL|HONDA FINANCIAL|TOYOTA FINANCIAL|FORD CREDIT|FORD MOTOR CREDIT|"
        "HYUNDAI CAPITAL|KIA FINANCE|MAZDA FINANCIAL|VW CREDIT|SUBARU CAPITAL|"
        "MERCEDES.BENZ FINANCIAL|BMW FINANCIAL|NISSAN MOTOR|ACURA FINANCIAL|LEXUS FINANCIAL",
        "car_loan", priority=100, is_regex=True, label="Major automaker financing arms",
    ),
    BundledRule(
        "RBC AUTO|TD AUTO|SCOTIA AUTO|CIBC AUTO|BMO AUTO|DESJARDINS AUTO|"
        "NATIONAL BANK AUTO|TANGERINE AUTO|EQUITABLE BANK AUTO",
        "car_loan", priority=100, is_regex=True, label="Canadian bank auto-loan products",
    ),
    BundledRule("AUTO LOAN|VEHICLE LOAN|CAR LOAN PAYMENT|CAR FINANCE", "car_loan", priority=300, label="Generic auto-loan catch-all"),
]


# ===========================================================================
# SHOPPING
# ===========================================================================

SHOPPING_RULES: list[BundledRule] = [
    # Clothing
    BundledRule(
        "H&M|H & M|UNIQLO|ZARA\\b|GAP\\b|OLD NAVY|BANANA REPUBLIC|J\\.CREW|J CREW|"
        "ARITZIA|LULULEMON|ROOTS CANADA|REITMANS|RICKI.S|TIP TOP|MOORES|SUITSUPPLY|"
        "UNIQLO|FOOT LOCKER|SPORT CHEK|ATMOSPHERE|MARK.S WORK|WINNERS\\b|MARSHALLS",
        "clothing", priority=200, is_regex=True,
    ),
    BundledRule("NIKE\\b|ADIDAS|UNDER ARMOUR|PUMA\\b|NEW BALANCE|CONVERSE|REEBOK|VANS\\b|ASICS|BROOKS RUNNING", "clothing", priority=220, is_regex=True),
    BundledRule("MUJI|\\bJOURNEYS\\b", "clothing", priority=220, is_regex=True, label="Muji + Journeys"),
    BundledRule("CLOTHING|APPAREL|BOUTIQUE|MENS WEAR|WOMENS WEAR", "clothing", priority=600),

    # Electronics
    BundledRule(
        "BEST BUY|APPLE STORE|MICROSOFT STORE|NEWEGG|MEMORY EXPRESS|CANADA COMPUTERS|"
        "THE SOURCE|MICRO CENTER|STAPLES|STAPLES BUSINESS",
        "electronics", priority=200,
    ),
    BundledRule("ELECTRONICS|COMPUTER STORE|MOBILE SHOP", "electronics", priority=600),

    # Home Goods
    BundledRule(
        "IKEA\\b|HOMESENSE|STRUCTUBE|EQ3\\b|CRATE.AND.BARREL|POTTERY BARN|WEST ELM|CB2\\b|"
        "BED BATH BEYOND|RESTORATION HARDWARE|WAYFAIR|LINEN CHEST",
        "home_goods", priority=200, is_regex=True,
    ),

    # Misc (generic shopping)
    BundledRule("DOLLARAMA|DOLLAR TREE|DOLLAR STORE|GIANT TIGER", "misc", priority=200),
    BundledRule("WALMART|WAL.?MART|HUDSON.S BAY|THE BAY\\b|CANADIAN TIRE\\b", "misc", priority=300, is_regex=True),
    # Garden centers / nurseries — Plant Society is a Toronto plant-shop chain
    # (not cannabis-related despite the name). Misc is the cleanest default
    # for plants-as-decor; users with a Hobbies framing can override per-row.
    BundledRule("PLANT SOCIETY|SHERIDAN NURSERIES|HUMBER NURSERIES|TERRA GREENHOUSES", "misc", priority=200, label="Garden centers / plant shops"),
    # Amazon retail — avoid matching:
    #  * the bare `AMZN` ticker on IBKR option/equity trades (`AMZN 15MAY26 235 C`)
    #  * the broker-side stock description `AMAZON.COM INC` (negative lookahead on ` INC`)
    BundledRule(
        "AMAZON\\.CA|AMAZON\\.COM(?!\\s+INC\\b)|AMAZON\\*|AMZN MKTP|AMZN DIGITAL",
        "online_shopping", priority=300, is_regex=True, label="Amazon retail (excludes AMZN ticker + AMAZON.COM INC stock)",
    ),
    # Online marketplaces — `WAYFAIR(?! INC)` and `ETSY\b(?! INC)` keep broker-side
    # stock descriptions like `WAYFAIR INC` / `ETSY INC` out of Online Shopping.
    # SOCIETY6 (print-on-demand merch) and PAYPAL *WESTERNBIDI (online auction
    # merchant) are common US-billed online stores Canadians order from.
    BundledRule(
        "ETSY\\b(?!\\s+INC\\b)|EBAY\\b(?!\\s+INC\\b)|ALIEXPRESS|SHEIN|TEMU|WAYFAIR(?!\\s+INC\\b)|AMAZON PRIME(?! VIDEO)|"
        "SOCIETY6\\.COM|SOCIETY6|WESTERNBIDI",
        "online_shopping", priority=300, is_regex=True, label="Online marketplaces (excludes broker stock descriptions)",
    ),
    BundledRule("PERKOPOLIS", "online_shopping", priority=200, label="Perkopolis (corporate perks marketplace)"),
]


# ===========================================================================
# SUBSCRIPTIONS
# ===========================================================================

SUBSCRIPTIONS_RULES: list[BundledRule] = [
    # Video streaming services (audio/music services route to Entertainment > Music instead)
    BundledRule(
        "NETFLIX|DISNEY.?PLUS|DISNEY\\+|DISNEY\\*|CRAVE\\b|AMAZON PRIME|PRIME VIDEO|"
        "HULU|PARAMOUNT|HBO MAX|HBO\\b|MAX\\.|DAZN|TWITCH|MUBI|CRITERION|"
        "CURIOSITY STREAM|BRITBOX|ACORN TV|APPLE TV|YOUTUBE PREMIUM",
        "streaming", priority=200, is_regex=True, label="Video streaming subscriptions",
    ),

    # Apps & SaaS (was "Software" — clearer scope for the subscription bucket)
    BundledRule(
        "MICROSOFT 365|OFFICE 365|MICROSOFT\\*|GITHUB|GITLAB|FIGMA|NOTION|EVERNOTE|"
        "ADOBE|CREATIVE CLOUD|DROPBOX|GOOGLE STORAGE|GOOGLE ONE|GOOGLE WORKSPACE|"
        "ICLOUD|APPLE\\.COM/BILL|APPLE\\.COM/CA|APPLE\\*|OPENAI|CHATGPT|ANTHROPIC|"
        "CLAUDE\\.AI|REPLIT|VSCO|CANVA|GRAMMARLY|1PASSWORD|LASTPASS|BITWARDEN|"
        "EXPRESS VPN|NORDVPN|SURFSHARK|RAYCAST|LINEAR\\.APP|VERCEL|HEROKU|"
        "DIGITALOCEAN|CLOUDFLARE|\\bAWS\\b|GOOGLE CLOUD|GCP\\b|JETBRAINS|INTELLIJ",
        "apps_and_saas", priority=200, is_regex=True,
    ),
    # Meta / Facebook paid services — boosted posts, Instagram ads, Marketplace
    # listing fees, Meta Verified. Per-charge unique transaction IDs (the
    # `FACEBK *XXXXX` form) mean apply-to-similar can't bulk-recategorize, so
    # they need a bundled rule. Not a perfect semantic fit (ad spend isn't
    # SaaS) but the closest existing leaf for "money paid to Meta's platform".
    BundledRule(
        "\\bFACEBK\\b|\\bFACEBOOK\\b|META PLATFORMS|META PAY\\b|META\\*",
        "apps_and_saas", priority=200, is_regex=True, label="Meta / Facebook paid services",
    ),
    BundledRule("WEALTHICA", "apps_and_saas", priority=200, label="Wealthica (portfolio tracker)"),

    # Memberships (news, professional, club)
    BundledRule(
        "NEW YORK TIMES|NYT\\b|GLOBE AND MAIL|FINANCIAL TIMES|THE ATHLETIC|WSJ\\b|"
        "ECONOMIST|ARS TECHNICA|MEDIUM\\.COM|PATREON|SUBSTACK|"
        "COSTCO MEMBERSHIP|PRIME MEMBERSHIP|CAA\\b|AMA\\b|WALMART PLUS",
        "memberships", priority=200, is_regex=True,
    ),
]


# ===========================================================================
# ENTERTAINMENT
# ===========================================================================

ENTERTAINMENT_RULES: list[BundledRule] = [
    BundledRule("CINEPLEX|LANDMARK CINEMAS|IMAX|MOVIE THEATER|MOVIE THEATRE|TIFF\\b|FILM NOIR CINEMAS", "movies", priority=200, is_regex=True),
    BundledRule("CINEMA|MOVIES|FILMS\\b", "movies", priority=500, is_regex=True),
    BundledRule("TICKETMASTER|STUBHUB|EVENTBRITE|SHOWPASS|LIVE NATION|UNIVERSE\\.COM|FRINGE", "events", priority=200, is_regex=True),
    BundledRule("STEAM\\b|EPIC GAMES|NINTENDO|PLAYSTATION|XBOX\\b|RIOT GAMES|BLIZZARD|EA GAMES|UBISOFT|GOG\\.COM|HUMBLE BUNDLE|ROBLOX", "games", priority=200, is_regex=True),
    BundledRule("GAMEFOUND|BACKERKIT", "games", priority=150, label="Tabletop crowdfunding (board games)"),
    # Board-game cafés — venues where you pay to play tabletop games.
    BundledRule("SNAKES.{0,3}LATTES", "games", priority=200, is_regex=True, label="Board-game cafés"),
    BundledRule("MICHAELS|FABRICLAND|HOBBY|CRAFT STORE|ART STORE|DESERONTO|THE BEADERY", "hobbies", priority=200),
    # Dance studios — recurring class/membership fees. Grouped as Hobbies
    # (not Fitness) per user preference: dancing-as-hobby framing rather
    # than dancing-as-workout.
    BundledRule("STEPS DANCE STUDIO|AFROLATINO DANCE", "hobbies", priority=200, label="Dance studios"),
    BundledRule("INDIGO|CHAPTERS|AMAZON KINDLE|KINDLE STORE|BOOK DEPOSITORY|BOOKSTORE|SWEET PICKLE BOOKS", "books", priority=200),
    BundledRule("LONG & MCQUADE|SWEETWATER|MOOG MUSIC|GUITAR CENTER|STEVES MUSIC", "music", priority=200, label="Music gear"),
    # Music subscriptions route to Music (Entertainment) — content-type framing,
    # so total music spending (subscriptions + concerts + one-off purchases)
    # rolls up to a single number. Users who prefer subscription-bucket framing
    # can override per-service via user rules.
    BundledRule("SPOTIFY|APPLE MUSIC|YOUTUBE MUSIC|TIDAL\\b|PANDORA|SOUNDCLOUD|DEEZER|AMAZON MUSIC", "music", priority=180, is_regex=True, label="Music streaming subscriptions"),
]


# ===========================================================================
# HEALTH & WELLNESS
# ===========================================================================

HEALTH_RULES: list[BundledRule] = [
    # `SHOPPERS\s*DRUG\s*MART` catches both `SHOPPERS DRUG MART` (spaced) and
    # the squished POS form `SHOPPERSDRUGMART0943 TORONTO ON` (no spaces).
    BundledRule(
        "SHOPPERS\\s*DRUG\\s*MART|REXALL|JEAN COUTU|PHARMASAVE|LONDON DRUGS|UNIPRIX|"
        "LAWTONS|GUARDIAN PHARMACY|NUTRITION HOUSE",
        "pharmacy", priority=200, is_regex=True,
    ),
    BundledRule("PHARMACY|PHARMACIE|DRUG MART|PHARMA\\s", "pharmacy", priority=500, is_regex=True),

    BundledRule("CITY OASIS DENTAL|DENTIST|DENTAL CLINIC|DENTAL OFFICE|ORTHODONTIC|ORAL HEALTH", "dental", priority=200),
    BundledRule("DENTAL|DENTIST", "dental", priority=500),

    BundledRule("HAKIM OPTICAL|LENSCRAFTERS|FYIDOCTORS|VISIONS|EYE EXAM|OPTOMETRY|OPTICAL", "vision", priority=200, label="Eye care"),

    BundledRule("GOODLIFE|FIT4LESS|YMCA|LIFE TIME|F45|ORANGE THEORY|ANYTIME FITNESS|HONE FITNESS|EQUINOX|PURE BARRE|CROSSFIT|YOGA STUDIO|PILATES STUDIO|MARTIAL ARTS", "fitness", priority=200),
    # Popeye's Supplements is a Canadian supplements/sports-nutrition chain
    # (unrelated to Popeyes Chicken, which is already in Fast Food). The
    # apostrophe + ` SUPPLEMENT` suffix discriminates from the chicken chain.
    BundledRule("POPEYE.?S SUPPLEMENT", "fitness", priority=180, is_regex=True, label="Popeye's Supplements (sports nutrition)"),
    BundledRule("GYM\\b|FITNESS\\b|YOGA\\b|PILATES", "fitness", priority=500, is_regex=True),

    BundledRule("BEACHES THERAPY|THERAPY GROUP|COUNSELLING|COUNSELING|PSYCHOTHERAPY|PSYCHOLOGIST", "therapy", priority=200),

    BundledRule("MEDICAL CLINIC|HEALTH CENTRE|FAMILY DOCTOR|FAMILY PRACTICE|WALK-IN CLINIC|MEDICAL CENTRE", "doctor", priority=300),
]


# ===========================================================================
# PERSONAL CARE
# ===========================================================================

PERSONAL_CARE_RULES: list[BundledRule] = [
    BundledRule("GREAT CLIPS|FIRST CHOICE HAIRCUTTERS|SUPERCUTS|MAGICUTS|REGIS\\b|TONI&GUY|\\bTOPCUTS\\b|THE CUTTING ROOM", "hair", priority=200, is_regex=True),
    BundledRule("BARBER|HAIR SALON|HAIRCUT|HAIR STUDIO", "hair", priority=500),

    BundledRule("SEPHORA|MAC COSMETICS|ULTA\\b|SHOPPERS BEAUTY BOUTIQUE|NAIL SALON|NAIL BAR|WAXING|LASER HAIR", "beauty", priority=200, is_regex=True),

    BundledRule("SPA\\b|MASSAGE THERAPY|RMT\\b|BODYWORK", "spa", priority=300, is_regex=True),
]


# ===========================================================================
# TRAVEL
# ===========================================================================

TRAVEL_RULES: list[BundledRule] = [
    BundledRule(
        # AIRCANADA (no-space) catches Air Canada's Winnipeg ticketing-hub
        # descriptors for post-booking ancillary charges like
        # `AIRCANADA WINNIPEG ROUTING: FROM: TORONTO LESTER B P` (baggage,
        # seat selection, change fees). AIR CANADA (with space) catches the
        # primary ticket purchase descriptor.
        "AIR CANADA|AIRCANADA\\b|WESTJET|PORTER AIRLINES|\\bPORTER AI\\b|FLAIR AIRLINES|SWOOP\\b|CANADA JETLINES|TRANSAT|"
        "UNITED AIRLINES|DELTA AIR|AMERICAN AIRLINES|ALASKA AIR|JETBLUE|LUFTHANSA|KLM\\b|"
        "AIR FRANCE|BRITISH AIRWAYS|EMIRATES|QATAR AIRWAYS|TURKISH AIRLINES|RYANAIR|"
        "EASYJET|WIZZ AIR|UA AIR\\b|AC\\s+FLIGHT",
        "flights", priority=200, is_regex=True,
    ),
    BundledRule("EXPEDIA|TRAVELOCITY|KAYAK|FLIGHTHUB|SUNWING|GOOGLE TRAVEL", "flights", priority=250),

    BundledRule(
        "MARRIOTT|HILTON|HYATT|INTERCONTINENTAL|FOUR SEASONS|FAIRMONT|SHERATON|RADISSON|"
        "HOLIDAY INN|BEST WESTERN|HAMPTON INN|AIRBNB|VRBO|BOOKING\\.COM|HOTELS\\.COM|"
        "AGODA|HOSTELWORLD",
        "lodging", priority=200, is_regex=True,
    ),
    BundledRule("HOTEL\\s|MOTEL|HOSTEL|B&B\\b|INN ", "lodging", priority=500, is_regex=True),
]


# ===========================================================================
# LIFE
# ===========================================================================

LIFE_RULES: list[BundledRule] = [
    BundledRule(
        "UNICEF|WORLD VISION|RED CROSS|CANADAHELPS|SALVATION ARMY|"
        "DOCTORS WITHOUT BORDERS|MSF\\b|UNITED WAY|HABITAT FOR HUMANITY|"
        "HEART AND STROKE|CANCER SOCIETY|WWF\\b|GREENPEACE|FOOD BANK",
        "charity", priority=200, is_regex=True,
    ),
    BundledRule("DONATION|CHARITABLE|CHARITY", "charity", priority=500),

    BundledRule("UDEMY|COURSERA|EDX\\b|SKILLSHARE|LINKEDIN LEARNING|PLURALSIGHT|KHAN ACADEMY|DUOLINGO|MASTERCLASS", "education", priority=200, is_regex=True),
    BundledRule("TUITION|UNIVERSITY OF|COLLEGE OF|STUDENT FEES", "education", priority=300),

    BundledRule("DAYCARE|MONTESSORI|PRESCHOOL|TOYS R US|MASTERMIND TOYS|JOE FRESH KIDS|CARTER.?S\\b", "kids", priority=200, is_regex=True),

    BundledRule(
        "PETSMART|PET VALU|PETVALU|REN.S PETS|GLOBAL PET|CHEWY\\b|"
        "ANIMAL HOSPITAL|VETERINARY|VET CLINIC|PET GROOMING",
        "pets", priority=200, is_regex=True,
    ),

    # Student loans — Canadian student-loan programs + generic patterns
    BundledRule("NSLSC\\b|CSL PAYMENT|CANADA STUDENT LOAN|OSAP\\b|\\bAFE\\b|ALBERTA STUDENT|BC STUDENT LOAN|SASKATCHEWAN STUDENT", "student_loan", priority=100, is_regex=True, label="Canadian student-loan programs"),
    BundledRule("STUDENT LOAN PAYMENT|STUDENT LOAN PYMT|STUDENT LOAN PMT", "student_loan", priority=200),
    BundledRule("STUDENT LOAN", "student_loan", priority=400, label="Generic student loan catch-all"),
]


# ===========================================================================
# TAXES
# ===========================================================================

TAX_RULES: list[BundledRule] = [
    BundledRule("CRA\\b|CANADA REVENUE AGENCY|REVENU QUEBEC|RQ\\b|INCOME TAX PAYMENT", "income_tax", priority=200, is_regex=True),
    BundledRule("PROPERTY TAX|REALTY TAX|MUNICIPAL TAX", "property_tax", priority=200),
    BundledRule("PROVINCIAL TAX|HST PAYMENT|GST PAYMENT|HARMONIZED SALES TAX", "other_tax", priority=200),

    # Tax preparation services — accountant / CPA fees and tax-software
    # subscriptions. `\bCPA\b` and `\bACCOUNTANT\b` are word-bounded to avoid
    # matching inside other words. Small local firms (e.g. "S & A PARTNERS PC")
    # aren't easily generalizable — tag those manually once and let
    # apply-to-similar fan out if the descriptor is stable.
    BundledRule(
        "TURBOTAX|H&R BLOCK|H AND R BLOCK|WEALTHSIMPLE TAX|\\bUFILE\\b|TAXACT|"
        "\\bACCOUNTANT\\b|\\bCPA\\b|TAX SERVICES|TAX PREPARATION",
        "tax_prep", priority=200, is_regex=True, label="Tax-prep software + professional fees",
    ),
]


# ===========================================================================
# OTHER
# ===========================================================================

OTHER_RULES: list[BundledRule] = [
    # ATM/ABM cash withdrawals -> Cash (neutral: money moved from bank to
    # wallet). `\bABM\b` catches the Canadian "Automated Banking Machine"
    # wording (Tangerine: `Withdrawal - Scotiabank ABM`, `Withdrawal - ABM - EL
    # RANCHO ...`). Word-bounded so it doesn't fire inside other words (e.g.
    # foreign merchant names that happen to contain the letters A-B-M).
    # Outflows only (`amount_sign='negative'`) so an ATM cash *deposit* (cash
    # leaving your wallet) isn't tagged Cash and doesn't spawn a +mirror leg.
    BundledRule(
        "ATM WITHDRAWAL|CASH WITHDRAWAL|^CASH$|WITHDRAWAL.*ATM|"
        "ATM CASH|\\bABM\\b",
        "cash", amount_sign="negative", priority=300, is_regex=True,
        label="ATM/ABM cash withdrawals",
    ),
    # ATM/ABM cash *deposits* -> Cash (neutral: money moved from wallet to bank).
    # Inflows only (`amount_sign='positive'`) and deposit-specific wording so the
    # broad `DEPOSIT` rules (paycheck/EFT/etc.) aren't swept in. The mirror leg is
    # a −amount on the cash holder (cash leaving your wallet).
    BundledRule(
        "CASH DEPOSIT|ATM DEPOSIT|ABM DEPOSIT|DEPOSIT.*\\bATM\\b|DEPOSIT.*\\bABM\\b",
        "cash", amount_sign="positive", priority=300, is_regex=True,
        label="ATM/ABM cash deposits",
    ),
    # HealthOne Rehab — rehab/physio clinic. Per maintainer preference: tagged
    # under Health & Wellness > Other (seed_key 'health_other', added in
    # migration f2a3b4c5d6e7) rather than Therapy or Doctor.
    BundledRule("HEALTHONE REHAB|HEALTH.?ONE REHAB", "health_other", priority=200, is_regex=True, label="HealthOne Rehab clinic"),
    # Government service fees — passport / driver's licence / vehicle reg /
    # health card / SIN / citizenship card. `\bMTO\b` and `\bIRCC\b` are
    # word-bounded to avoid matching inside other words. Not a tax — these
    # are administrative service fees that fund the issuing department.
    BundledRule(
        "SERVICE CANADA|SERVICEONTARIO|SERVICE ONTARIO|\\bMTO\\b|\\bIRCC\\b|"
        "MINISTRY OF TRANSPORTATION|MINISTRY OF GOVERNMENT|PASSPORT CANADA|"
        "CITIZENSHIP CANADA|HEALTH CARD ONTARIO|VEHICLE REGISTRATION",
        "government", priority=200, is_regex=True, label="Canadian government service fees",
    ),
]


# ===========================================================================
# MAINTAINER-CANONICALIZED OVERRIDES
# ===========================================================================
# Generalizable merchant / payer / bank-flow categorizations promoted from the
# maintainer's own manual tagging — descriptors the shipped rules previously got
# wrong or didn't cover. Hyper-local single-location spots and personal one-offs
# are intentionally NOT bundled (they stay Uncategorized for the user to tag).
MAINTAINER_OVERRIDE_RULES: list[BundledRule] = [
    # Income payers (Tangerine "EFT Deposit from …" phrasing)
    BundledRule("EFT Deposit from CITY OF TORONTO", "paycheck", priority=50, provider="tangerine", label="City of Toronto payroll"),
    BundledRule("EFT Deposit from SWITCH HEALTH", "paycheck", priority=50, provider="tangerine", label="Switch Health payroll"),
    BundledRule("EFT Deposit from TEKSYSTEMS", "paycheck", priority=50, provider="tangerine", label="TEKsystems payroll"),
    # Own-account / card / brokerage money movement
    BundledRule("AMEX BILL PYMT", "cc_payment", priority=80, provider="tangerine", label="Amex bill payment from Tangerine"),
    BundledRule("Funds Transfer - Questrade|Questrade Visa Direct", "transfer", priority=90, provider="tangerine", label="Questrade funding"),
    BundledRule("PAYMENT - TANGERINE", "cc_payment", priority=80, provider="scotiabank", label="Scotia card payment from Tangerine"),
    # NOTE: Wise "Topped up account" intentionally NOT bundled — it's own-money
    # funding (categories_rules line ~141 already maps it to Transfer). The
    # maintainer tagged it Deposit, but Deposit is income-classified, so forcing
    # it globally would count every Wise user's top-up as income.
    BundledRule("Direct deposit from Tangerine", "transfer", priority=55, provider="eqbank", label="EQ <- Tangerine own-account transfer (beats generic Direct-deposit payroll rule)"),
    # National / regional merchants (provider-agnostic)
    BundledRule("CANADIAN TIRE #|CANADIAN TIRE STORE|CDN TIRE", "misc", priority=150, label="Canadian Tire retail (beats the TIRE\\s car-repair regex; CANADIAN TIRE GAS stays Gas)"),
    BundledRule("AMAZON.CA PRIME MEMBER|AMAZON PRIME MEMBER|AMAZON.COM PRIME MEMBER", "memberships", priority=150, label="Amazon Prime membership (beats Amazon-retail / streaming rules)"),
    BundledRule("ROGERS", "internet", priority=150, provider="scotiabank", label="Rogers (maintainer's Scotia-billed Rogers line is internet, not mobile)"),
    BundledRule("FEDEX", "shipping", priority=200, label="FedEx"),
    BundledRule("CHICK-FIL-A|CHICK FIL A|CHICKFILA", "fast_food", priority=200, label="Chick-fil-A"),
    BundledRule("RABBA", "groceries", priority=200, label="Rabba (Toronto convenience grocer)"),
    BundledRule("MARK'S STORE|MARKS WORK WEARHOUSE", "clothing", priority=200, label="Mark's apparel"),
    BundledRule("7 ELEVEN STORE|7-ELEVEN STORE", "groceries", priority=200, label="7-Eleven convenience (GAS stays Gas)"),
    BundledRule("SPARKLE SOLUTIONS", "laundry", priority=200, label="Sparkle Solutions laundry machines"),
    BundledRule("KOJIN", "hair", priority=200, label="Kojin's grooming studio (hair, not pets)"),
]


# ===========================================================================
# GENERIC KEYWORD FALLBACKS
# ===========================================================================
# Low-priority, high-confidence category keywords so an unknown local merchant
# still gets categorized from a descriptive word in its name (a no-name pub in
# Manitoba → Restaurants) instead of falling through to Uncategorized. They sit
# below every merchant/provider rule and the existing generic rules, but above
# the type-mapping / last-resort fallbacks. Kept deliberately confident — clearly
# category-indicating words only; ambiguous ones (MARKET, WINERY, THAI) are left
# out so they don't mis-tag.
GENERIC_KEYWORD_FALLBACK_RULES: list[BundledRule] = [
    BundledRule("BURGER\\b|SHAWARMA|FALAFEL|TAQUERIA|POUTINE|FISH & CHIPS|FISH AND CHIPS|NOODLE|DUMPLING|SANDWICH", "fast_food", priority=600, is_regex=True, label="Generic quick-service keywords"),
    BundledRule("BAKERY|PATISSERIE|GELATO|CREAMERY|CHOCOLATIER|DESSERT BAR|ICE CREAM|DONUT", "treats", priority=600, is_regex=True, label="Generic bakery/dessert keywords"),
    BundledRule("PHYSIOTHERAP|PHYSIO CLINIC|REHAB CLINIC|REHAB CENTRE", "health_other", priority=600, label="Generic physiotherapy/rehab keywords (generic clinics already route to Doctor)"),
    BundledRule("MUSEUM|CONCERT HALL|BOX OFFICE|AMPHITHEATRE|AMPHITHEATER", "events", priority=600, is_regex=True, label="Generic event-venue keywords"),
]


# ===========================================================================
# Combined rulepack (priority-sorted at lookup)
# ===========================================================================

BUNDLED_RULES: tuple[BundledRule, ...] = tuple(
    INCOME_RULES
    + INVESTMENT_RULES
    + TRANSFER_RULES
    + FINANCIAL_RULES
    + FOOD_RULES
    + HOUSING_RULES
    + TRANSPORTATION_RULES
    + SHOPPING_RULES
    + SUBSCRIPTIONS_RULES
    + ENTERTAINMENT_RULES
    + HEALTH_RULES
    + PERSONAL_CARE_RULES
    + TRAVEL_RULES
    + LIFE_RULES
    + TAX_RULES
    + OTHER_RULES
    + MAINTAINER_OVERRIDE_RULES
    + GENERIC_KEYWORD_FALLBACK_RULES
)


# Pre-sort by priority once at import (lower priority value = higher precedence)
BUNDLED_RULES_SORTED: tuple[BundledRule, ...] = tuple(
    sorted(BUNDLED_RULES, key=lambda r: r.priority)
)


# ===========================================================================
# Matcher
# ===========================================================================

_REGEX_CACHE: dict[str, re.Pattern[str]] = {}


def _compiled(pattern: str) -> re.Pattern[str]:
    cached = _REGEX_CACHE.get(pattern)
    if cached is not None:
        return cached
    compiled = re.compile(pattern, re.IGNORECASE)
    _REGEX_CACHE[pattern] = compiled
    return compiled


def bundled_rule_matches(
    rule: BundledRule,
    *,
    description: str | None,
    amount: float | None,
    provider: str | None,
    account_type: str | None,
    transaction_type: str | None,
) -> bool:
    if rule.provider and rule.provider.lower() != (provider or "").lower():
        return False
    if rule.account_type and rule.account_type != account_type:
        return False
    if rule.transaction_type and rule.transaction_type != transaction_type:
        return False
    if rule.amount_sign and amount is not None:
        if rule.amount_sign == "positive" and amount <= 0:
            return False
        if rule.amount_sign == "negative" and amount >= 0:
            return False
    # Normalize None description to empty string for matching so scope-only
    # rules (e.g. provider="coinbase" + transaction_type="withdrawal" with
    # pattern ".*") still fire on connector rows that don't ship a
    # description. Substring/regex patterns that need actual content won't
    # match empty strings anyway, so this is safe for the existing rulepack.
    description = description or ""
    if rule.is_regex:
        return bool(_compiled(rule.pattern).search(description))
    # Apostrophe-insensitive substring match so apostrophe-free patterns
    # ("LONGOS", "NO FRILLS") still catch the provider's apostrophe'd
    # descriptions ("LONGO'S", "NO FRILL'S"). Substring path only; no bundled
    # rule pattern relies on a literal apostrophe.
    desc_lower = description.lower().replace("'", "").replace("’", "")
    for alt in rule.pattern.lower().split("|"):
        alt = alt.strip().replace("'", "").replace("’", "")
        if alt and alt in desc_lower:
            return True
    return False


# ===========================================================================
# Merchant Category Codes (MCC)
# ===========================================================================
# Some card connectors (e.g. Scotiabank's `merchant.categoryCode`) supply the
# Visa/Mastercard MCC per transaction. It's a strong categorization signal —
# especially for merchants not in the name rulepack above — so the resolver
# consults it AFTER name rules and BEFORE the generic type-mapping fallback
# (which would otherwise file a card purchase as a Withdrawal). Only SPENDING
# MCCs are mapped; financial / transfer / money-movement codes (6012, 6051,
# 6536…) are intentionally omitted so genuine transfers still fall through to
# type-mapping. Coarse by nature (4814 = all telecom, can't split phone vs
# internet), so it's a fallback, not a replacement for name rules or a per-user
# rule. Values are `category_seed_key`s that must exist in SEED_TAXONOMY.
MCC_TO_SEED_KEY: dict[str, str] = {
    # Food & drink
    "5411": "groceries", "5422": "groceries", "5451": "groceries",
    "5499": "groceries", "5300": "groceries",
    "5441": "treats", "5462": "treats",
    "5811": "restaurants", "5812": "restaurants",
    "5814": "fast_food",
    "5813": "alcohol", "5921": "alcohol",
    # Transportation
    "5172": "gas", "5541": "gas", "5542": "gas",
    "4111": "transit", "4112": "transit", "4131": "transit",
    "4121": "ride_share",
    "7523": "parking", "4784": "tolls",
    "5533": "car_repair", "7531": "car_repair", "7538": "car_repair",
    # Travel
    "4511": "flights", "7011": "lodging",
    # Housing / utilities / telecom
    "4900": "utilities", "4814": "phone", "4899": "streaming",
    "6513": "rent",  # real-estate agents/managers — rentals (rent collectors like Chexy)
    "5200": "maintenance", "5211": "maintenance", "5231": "maintenance", "5251": "maintenance",
    # Shopping
    "5611": "clothing", "5621": "clothing", "5631": "clothing", "5641": "clothing",
    "5651": "clothing", "5661": "clothing", "5691": "clothing", "5699": "clothing",
    "5045": "electronics", "5732": "electronics", "5734": "electronics", "5946": "electronics",
    "5712": "home_goods", "5713": "home_goods", "5714": "home_goods",
    "5719": "home_goods", "5722": "home_goods",
    "5310": "misc", "5311": "misc", "5331": "misc", "5399": "misc", "5999": "misc",
    "5964": "online_shopping", "5965": "online_shopping", "5969": "online_shopping",
    # Subscriptions / digital goods
    "5815": "streaming", "5816": "games", "5817": "apps_and_saas", "5818": "apps_and_saas",
    # Entertainment
    "7832": "movies", "7922": "events", "7929": "events",
    "5945": "hobbies", "5970": "hobbies",
    "5733": "music", "5735": "music",
    "5192": "books", "5942": "books",
    # Health & wellness
    "5912": "pharmacy",
    "8011": "doctor", "8062": "doctor", "8099": "doctor",
    "8021": "dental", "8042": "vision", "7997": "fitness",
    # Personal care
    "7230": "hair", "7298": "spa",
    # Life
    "0742": "pets", "5995": "pets",
    "8211": "education", "8220": "education", "8241": "education",
    "8244": "education", "8249": "education", "8299": "education",
    "8398": "charity",
    "5960": "insurance", "6300": "insurance",
    # Taxes / government
    "9311": "income_tax", "9399": "government",
    # NB: financial-institution cash MCCs 6010 (manual disbursement) and 6011
    # (ATM) are intentionally NOT mapped. Scotia tags credit-card payments and
    # cheques with 6010, so mapping them to Cash (an expense) would mis-file
    # transfers as spend. Named ATM/cash withdrawals still resolve to Cash via
    # the bundled name rule; unnamed financial-institution movements fall
    # through to type-mapping (Deposit/Withdrawal = transfer, excluded from spend).
}


def seed_key_for_mcc(mcc: str | None) -> str | None:
    """Map a Visa/Mastercard MCC to a spending `category_seed_key`, or None.

    Exact 4-digit codes win; otherwise the brand-specific airline (3000–3299)
    and lodging (3501–3999) ranges collapse to flights / lodging."""
    if not mcc:
        return None
    code = str(mcc).strip()
    if not code.isdigit():
        return None
    seed = MCC_TO_SEED_KEY.get(code)
    if seed:
        return seed
    n = int(code)
    if 3000 <= n <= 3299:
        return "flights"
    if 3501 <= n <= 3999:
        return "lodging"
    return None
