"""Business rules over one validation run (docs/business-rules.md).

Input: the validator's exchange log and contract results (JSONL). Output, as DataFrames:

- bronze: exchanges, contract_results (as read, typed);
- silver: consent_reads, balances, listing_pages, listing_items (parsed from the responses
  that passed the contract layer: verdict pass or warn);
- gold: business_checks, one row per rule evaluated on an exchange, with outcome pass, finding
  or inconclusive.

Pure DataFrame code: the job (job.py) does the reading and the Iceberg writes, the tests feed
exported runs from local files. Runs on the Spark image's Python 3.10.
"""

from __future__ import annotations

from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

ACCOUNTS_LIST = "accountsGetAccounts"
BALANCES = "accountsGetAccountsAccountIdBalances"
TRANSACTIONS = "accountsGetAccountsAccountIdTransactions"
TRANSACTIONS_CURRENT = "accountsGetAccountsAccountIdTransactionsCurrent"
# Only reads: the POST answers AWAITING_AUTHORISATION, and the authorisation happens outside the API.
CONSENT_READS = ["consentsGetConsentsConsentId"]
LISTINGS = [ACCOUNTS_LIST, TRANSACTIONS, TRANSACTIONS_CURRENT]
TRANSACTION_LISTINGS = [TRANSACTIONS, TRANSACTIONS_CURRENT]
# The specs' default page-size.
DEFAULT_PAGE_SIZE = 25
AMOUNT = T.DecimalType(20, 4)

S = T.StringType()
EXCHANGE_SCHEMA = T.StructType([
    T.StructField("run_id", S), T.StructField("walk_id", S), T.StructField("seq", T.LongType()),
    T.StructField("ts", S), T.StructField("interaction_id", S), T.StructField("method", S),
    T.StructField("path", S), T.StructField("query", S), T.StructField("operation", S),
    T.StructField("consent", S), T.StructField("request_headers", T.MapType(S, S)),
    T.StructField("request_body", S), T.StructField("status", T.IntegerType()),
    T.StructField("response_headers", T.MapType(S, S)), T.StructField("response_body", S),
])
FINDING = T.StructType([T.StructField(n, S) for n in ("rule", "severity", "pointer", "message")])
CONTRACT_SCHEMA = T.StructType([
    T.StructField("run_id", S), T.StructField("walk_id", S), T.StructField("seq", T.LongType()),
    T.StructField("ts", S), T.StructField("interaction_id", S), T.StructField("method", S),
    T.StructField("path", S), T.StructField("query", S), T.StructField("api", S),
    T.StructField("operation", S), T.StructField("status", T.IntegerType()), T.StructField("verdict", S),
    T.StructField("rules", T.ArrayType(S)), T.StructField("findings", T.ArrayType(FINDING)),
])
CHECK_COLUMNS = ["run_id", "walk_id", "seq", "interaction_id", "operation", "rule", "outcome", "pointer", "detail"]

# What the rules read from the response bodies (from_json leaves the rest out).
CONSENT_BODY = T.StructType([T.StructField("data", T.StructType([
    T.StructField("consentId", S), T.StructField("status", S), T.StructField("expirationDateTime", S)]))])
BALANCE_BODY = T.StructType([T.StructField("data", T.StructType([
    T.StructField("availableAmount", T.StructType([T.StructField("amount", S)]))]))])
LISTING_BODY = T.StructType([
    T.StructField("data", T.ArrayType(T.StructType([
        T.StructField("accountId", S), T.StructField("transactionId", S),
        T.StructField("creditDebitType", S), T.StructField("completedAuthorisedPaymentType", S),
        T.StructField("transactionAmount", T.StructType([T.StructField("amount", S)])),
        T.StructField("transactionDateTime", S),
    ]))),
    T.StructField("links", T.StructType([T.StructField("next", S)])),
    T.StructField("meta", T.StructType([T.StructField("totalRecords", T.LongType()),
                                        T.StructField("totalPages", T.LongType())])),
])


def _param(name: str) -> Column:
    return F.nullif(F.regexp_extract(F.coalesce(F.col("query"), F.lit("")), rf"(?:^|&){name}=([^&]*)", 1), F.lit(""))


def _account_id() -> Column:
    return F.nullif(F.regexp_extract("path", r"/accounts/v\d+/accounts/([^/]+)", 1), F.lit(""))


def _listing() -> Column:
    """The listing an exchange belongs to: walk, consent, path, page size and the query apart
    from page."""
    rest = F.regexp_replace(F.regexp_replace(F.coalesce(F.col("query"), F.lit("")), r"(^|&)(page|page-size)=[^&]*", ""), r"^&", "")
    return F.concat_ws("|", "walk_id", F.coalesce("consent", F.lit("")), "path", rest, _page_size().cast("string"))


def _page_size() -> Column:
    return F.coalesce(_param("page-size").cast("long"), F.lit(DEFAULT_PAGE_SIZE))


def _page() -> Column:
    return F.coalesce(_param("page").cast("long"), F.lit(1))


def bronze(exchanges: DataFrame, contract: DataFrame) -> dict[str, DataFrame]:
    return {
        "exchanges": exchanges.withColumn("ts", F.to_timestamp("ts")),
        "contract_results": contract.withColumn("ts", F.to_timestamp("ts")),
    }


def usable(exchanges: DataFrame, contract: DataFrame) -> DataFrame:
    """The 2xx exchanges the contract layer did not fail (the business layer's input)."""
    verdicts = contract.select("run_id", "interaction_id", F.col("verdict").alias("contract_verdict"))
    return (exchanges.join(verdicts, ["run_id", "interaction_id"], "left")
            .where((F.col("contract_verdict") != "fail") & F.col("status").between(200, 299)))


def silver(ok: DataFrame) -> dict[str, DataFrame]:
    keys = ["run_id", "walk_id", "seq", "interaction_id", "ts", "operation", "consent"]

    consents = (ok.where(F.col("operation").isin(CONSENT_READS))
                .withColumn("body", F.from_json("response_body", CONSENT_BODY))
                .select(*keys, F.col("body.data.consentId").alias("consent_id"),
                        F.col("body.data.status").alias("consent_status"),
                        F.to_timestamp("body.data.expirationDateTime").alias("expiration_ts"))
                .where(F.col("consent_id").isNotNull()))

    balances = (ok.where(F.col("operation") == BALANCES)
                .withColumn("body", F.from_json("response_body", BALANCE_BODY))
                .select(*keys, _account_id().alias("account_id"),
                        F.col("body.data.availableAmount.amount").cast(AMOUNT).alias("available_amount")))

    # A listing: the pages of one request across page numbers (same walk, consent, path, page
    # size and the other query parameters).
    pages = (ok.where(F.col("operation").isin(LISTINGS))
             .withColumn("body", F.from_json("response_body", LISTING_BODY))
             .select(*keys, _account_id().alias("account_id"), _listing().alias("listing"),
                     _page().alias("page"), _page_size().alias("page_size"),
                     F.to_date(_param("fromBookingDate")).alias("from_date"),
                     F.to_date(_param("toBookingDate")).alias("to_date"),
                     F.col("body.links.next").isNotNull().alias("has_next"),
                     F.col("body.meta.totalRecords").alias("total_records"),
                     F.col("body.meta.totalPages").alias("total_pages"),
                     F.coalesce(F.size("body.data"), F.lit(0)).alias("item_count"),
                     F.col("body.data").alias("data")))
    items = (pages.select("run_id", "walk_id", "interaction_id", "operation", "consent", "listing", "account_id", "page",
                          F.posexplode("data").alias("item_index", "item"))
             .select("run_id", "walk_id", "interaction_id", "operation", "consent", "listing", "account_id", "page",
                     "item_index",
                     F.coalesce("item.transactionId", "item.accountId").alias("item_id"),
                     F.col("item.creditDebitType").alias("credit_debit_type"),
                     F.col("item.completedAuthorisedPaymentType").alias("payment_type"),
                     F.col("item.transactionAmount.amount").cast(AMOUNT).alias("amount"),
                     F.col("item.transactionDateTime").alias("transaction_date_time")))
    return {"consent_reads": consents, "balances": balances, "listing_pages": pages.drop("data"), "listing_items": items}


def _check(df: DataFrame, rule: str, outcome: Column, pointer: Column, detail: Column) -> DataFrame:
    return df.select("run_id", "walk_id", "seq", "interaction_id", "operation", F.lit(rule).alias("rule"),
                     outcome.alias("outcome"), pointer.alias("pointer"), detail.alias("detail"))


def _when(finding: Column, inconclusive: Column | None = None) -> Column:
    outcome = F.when(finding, "finding").otherwise("pass")
    return outcome if inconclusive is None else F.when(inconclusive, "inconclusive").otherwise(outcome)


def listings(pages: DataFrame, items: DataFrame) -> DataFrame:
    """Per listing: complete (pages 1..n, the last without a next link), item count, duplicates."""
    last = Window.partitionBy("listing").orderBy(F.col("page").desc())
    summary = (pages.withColumn("is_last", F.row_number().over(last) == 1)
               .groupBy("run_id", "listing")
               .agg(F.min("page").alias("first_page"), F.max("page").alias("last_page"),
                    F.countDistinct("page").alias("pages"), F.sum("item_count").alias("items"),
                    F.max(F.when(F.col("is_last"), F.col("has_next"))).alias("last_has_next")))
    dupes = (items.groupBy("listing", "item_id").count().where("count > 1")
             .groupBy("listing").agg(F.count("*").alias("duplicated_ids")))
    return (summary.join(dupes, "listing", "left").fillna({"duplicated_ids": 0})
            .withColumn("complete", (F.col("first_page") == 1) & (F.col("pages") == F.col("last_page"))
                        & ~F.col("last_has_next")))


def gold(silver_tables: dict[str, DataFrame]) -> DataFrame:
    pages, items = silver_tables["listing_pages"], silver_tables["listing_items"]
    summary = listings(pages, items)

    # transactions.within-window: every transaction dated inside the requested booking window.
    tx_pages = pages.where(F.col("operation").isin(TRANSACTION_LISTINGS))
    outside = (items.where(F.col("operation").isin(TRANSACTION_LISTINGS))
               .join(tx_pages.select("interaction_id", "from_date", "to_date"), "interaction_id")
               .withColumn("day", F.to_date(F.substring("transaction_date_time", 1, 10)))
               .where((F.col("day") < F.col("from_date")) | (F.col("day") > F.col("to_date")))
               .groupBy("interaction_id")
               .agg(F.min("item_index").alias("first_outside"), F.count("*").alias("outside"),
                    F.min("transaction_date_time").alias("example")))
    window = tx_pages.join(outside, "interaction_id", "left")
    window_checks = _check(
        window, "transactions.within-window",
        _when(F.col("outside").isNotNull(), F.col("from_date").isNull() | F.col("to_date").isNull()),
        F.when(F.col("outside").isNotNull(), F.format_string("/data/%d/transactionDateTime", "first_outside")),
        F.when(F.col("outside").isNotNull(), F.format_string("%d transaction(s) outside %s..%s, e.g. %s", "outside",
                                                              F.col("from_date").cast("string"),
                                                              F.col("to_date").cast("string"), "example")))

    # pagination.duplicate-item: an item id already seen earlier in the listing.
    order = Window.partitionBy("listing", "item_id").orderBy("page", "item_index")
    repeated = (items.withColumn("occurrence", F.row_number().over(order)).where("occurrence > 1")
                .groupBy("interaction_id").agg(F.min("item_index").alias("first_repeat"),
                                               F.first("item_id").alias("repeated_id")))
    dup = pages.join(repeated, "interaction_id", "left")
    dup_checks = _check(dup, "pagination.duplicate-item", _when(F.col("first_repeat").isNotNull()),
                        F.when(F.col("first_repeat").isNotNull(), F.format_string("/data/%d", "first_repeat")),
                        F.when(F.col("first_repeat").isNotNull(), F.concat(F.lit("repeats "), "repeated_id")))

    # pagination.missing-next: no next link although a later page exists (totalPages, or a later
    # page of the listing with items, which the walk probes for). When the only later page was
    # left out by the contract layer, a full page without a next link cannot be judged.
    later = (pages.where("item_count > 0").select("listing", F.col("page").alias("later_page")))
    has_later = (pages.join(later, "listing").where(F.col("later_page") > F.col("page"))
                 .groupBy("interaction_id").agg(F.min("later_page").alias("later_page")))
    excluded = silver_tables["excluded_pages"].select("listing", F.col("page").alias("excluded_page"))
    has_excluded = (pages.join(excluded, "listing").where(F.col("excluded_page") > F.col("page"))
                    .groupBy("interaction_id").agg(F.min("excluded_page").alias("excluded_page")))
    nxt = pages.join(has_later, "interaction_id", "left").join(has_excluded, "interaction_id", "left")
    missing = ~F.col("has_next") & (F.col("later_page").isNotNull()
                                    | (F.col("total_pages").isNotNull() & (F.col("page") < F.col("total_pages"))))
    undecided = (~F.col("has_next") & (F.col("item_count") >= F.col("page_size"))
                 & F.col("excluded_page").isNotNull())
    next_checks = _check(nxt, "pagination.missing-next", _when(missing, ~missing & undecided),
                         F.when(missing, F.lit("/links/next")),
                         F.when(missing, F.format_string("page %d of the listing has no next link", "page")))

    # pagination.total-records: meta.totalRecords equals the items of the complete listing.
    totals = (pages.where(F.col("total_records").isNotNull())
              .join(summary.select("listing", "complete", "items"), "listing"))
    wrong_total = F.col("total_records") != F.col("items")
    total_checks = _check(totals, "pagination.total-records", _when(wrong_total, ~F.col("complete")),
                          F.when(F.col("complete") & wrong_total, F.lit("/meta/totalRecords")),
                          F.when(F.col("complete") & wrong_total,
                                 F.format_string("totalRecords %d, listing has %d", "total_records", "items")))

    # balance.matches-transactions: availableAmount equals the signed sum of the completed
    # transactions, over a complete transaction history of the account without repeated items.
    # The balance belongs to the account, so the history may come from any consent of the walk.
    history = summary.where(F.col("complete") & (F.col("duplicated_ids") == 0)).select("listing")
    signed = F.when(F.col("credit_debit_type") == "CREDITO", F.col("amount")).otherwise(-F.col("amount"))
    # Per usable history listing: its sum. An account can have several (page by page, and in one
    # page of 1000): the one with the largest pages is used.
    sums = (pages.where(F.col("operation") == TRANSACTIONS).join(history, "listing")
            .groupBy("walk_id", "account_id", "listing").agg(F.max("page_size").alias("page_size"))
            .join(items.where(F.col("payment_type") == "TRANSACAO_EFETIVADA")
                  .groupBy("listing").agg(F.sum(signed).alias("total")), "listing", "left")
            .withColumn("expected", F.coalesce("total", F.lit(0).cast(AMOUNT)))
            .withColumn("choice", F.row_number().over(Window.partitionBy("walk_id", "account_id")
                                                      .orderBy(F.col("page_size").desc(), "listing")))
            .where("choice = 1").select("walk_id", "account_id", "expected"))
    bal = silver_tables["balances"].join(sums, ["walk_id", "account_id"], "left")
    differs = F.col("available_amount") != F.col("expected")
    balance_checks = _check(bal, "balance.matches-transactions",
                            _when(differs, F.col("expected").isNull() | F.col("available_amount").isNull()),
                            F.when(differs, F.lit("/data/availableAmount/amount")),
                            F.when(differs, F.format_string("availableAmount %s, completed transactions add up to %s",
                                                            F.col("available_amount").cast("string"),
                                                            F.col("expected").cast("string"))))

    # consent.enforced: data is served only for a consent last read as AUTHORISED and not expired.
    calls = (silver_tables["accounts_calls"]).alias("c")
    reads = silver_tables["consent_reads"].alias("r")
    asof = (calls.join(reads, (F.col("c.walk_id") == F.col("r.walk_id")) & (F.col("c.consent") == F.col("r.consent_id"))
                       & (F.col("r.seq") < F.col("c.seq")), "left")
            .withColumn("rank", F.row_number().over(Window.partitionBy("c.interaction_id").orderBy(F.col("r.seq").desc_nulls_last())))
            .where("rank = 1")
            .select("c.run_id", "c.walk_id", "c.seq", "c.interaction_id", "c.operation", "c.ts",
                    F.col("r.consent_status").alias("consent_status"), F.col("r.expiration_ts").alias("expiration_ts"),
                    F.col("r.seq").alias("read_seq")))
    not_ok = (F.col("consent_status") != "AUTHORISED") | (F.col("expiration_ts").isNotNull() & (F.col("expiration_ts") < F.col("ts")))
    consent_checks = _check(asof, "consent.enforced", _when(not_ok, F.col("read_seq").isNull()),
                            F.when(not_ok, F.lit("status")),
                            F.when(not_ok, F.format_string("200 for a consent last read as %s", "consent_status")))

    return (window_checks.unionByName(dup_checks).unionByName(next_checks).unionByName(total_checks)
            .unionByName(balance_checks).unionByName(consent_checks).select(*CHECK_COLUMNS))


def build(exchanges: DataFrame, contract: DataFrame) -> dict[str, DataFrame]:
    """Every table of the run, by name."""
    tables = bronze(exchanges, contract)
    ok = usable(tables["exchanges"], tables["contract_results"])
    tables.update(silver(ok))
    accounts_calls = ok.where(F.col("operation").startswith("accountsGet") & (F.col("status") == 200)
                              & F.col("consent").isNotNull())
    # Listing pages the contract layer left out (any status): only their place in the listing.
    excluded_pages = (tables["exchanges"].join(ok.select("interaction_id"), "interaction_id", "left_anti")
                      .where(F.col("operation").isin(LISTINGS))
                      .select("interaction_id", _listing().alias("listing"), _page().alias("page")))
    tables["business_checks"] = gold({**tables, "accounts_calls": accounts_calls, "excluded_pages": excluded_pages})
    return tables
