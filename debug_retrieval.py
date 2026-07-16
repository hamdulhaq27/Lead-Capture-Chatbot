"""
debug_retrieval.py
===================
Test harness for the retrieval stage of rag_engine.py.

What changed vs. the original version:
- Test queries now cover every file in the expanded knowledge base
  (booking/cancellation policy, terms, privacy, payment, per-service
  detail pages, objections, glossary, industries) instead of just the
  original 3 files.
- Each query is labeled as expected in-domain or expected out-of-domain,
  so the script can auto-grade itself against MAX_DISTANCE_THRESHOLD
  instead of requiring you to eyeball every distance number.
- Added trickier edge cases: adjacent-but-different-agency topics,
  ambiguous short queries, empty/gibberish input, and queries that mix
  an in-domain and out-of-domain concept in one sentence.
- Prints a pass/fail summary at the end, plus a flat list of any
  misclassified queries so regressions are easy to spot after you
  change MAX_DISTANCE_THRESHOLD, the embedding model, or the KB content.
- Also runs a TOP_K impact analysis: compares TOP_K=4 (current) against a
  smaller candidate value for every in-domain query, and reports whether
  trimming would actually drop a distinct source (real information loss)
  or only redundant chunks from a source already included (safe to trim).

Usage
-----
python debug_retrieval.py                       # run everything
python debug_retrieval.py --verbose              # also show source text for each hit
python debug_retrieval.py --category booking     # only run one category
python debug_retrieval.py --candidate-topk 2     # compare against top_k=2 instead of 3
python debug_retrieval.py --skip-topk-analysis   # skip the TOP_K comparison section
"""

import sys
import argparse

sys.path.insert(0, '.')
from rag_engine import get_chroma_collection, retrieve, MAX_DISTANCE_THRESHOLD, TOP_K

# ---------------------------------------------------------------------------
# Test queries: (query, expect_in_domain, category)
#
# expect_in_domain=True  -> we expect the best-hit distance to clear the
#                            threshold (<= MAX_DISTANCE_THRESHOLD)
# expect_in_domain=False -> we expect it to NOT clear the threshold, i.e.
#                            the bot should fall back rather than answer
# ---------------------------------------------------------------------------
TEST_CASES = [
    # --- faq_and_policies.txt ---
    ("What are your policies?", True, "faq_policies"),
    ("What is your refund policy?", True, "faq_policies"),
    ("What are your business hours?", True, "faq_policies"),
    ("Do you require a long-term contract?", True, "faq_policies"),
    ("How do you report results to clients?", True, "faq_policies"),

    # --- pricing.txt ---
    ("How much does SEO cost?", True, "pricing"),
    ("What's the price for social media management?", True, "pricing"),
    ("Do you offer a discount for bundling services?", True, "pricing"),

    # --- services.txt ---
    ("What services do you offer?", True, "services"),
    ("Do you do branding and logo design?", True, "services"),

    # --- about_company.txt ---
    ("Tell me about your company.", True, "about"),
    ("What makes you different from other agencies?", True, "about"),

    # --- process_and_onboarding.txt ---
    ("What happens after I sign up?", True, "process"),
    ("How long does a website project take?", True, "process"),
    ("What do you need from me to get started?", True, "process"),

    # --- contact_and_team.txt ---
    ("How fast do you respond to emails?", True, "contact"),
    ("Who will be my main point of contact?", True, "contact"),
    ("What if there's an urgent issue with my live campaign?", True, "contact"),

    # --- case_studies.txt ---
    ("Do you have any case studies or results to show?", True, "case_studies"),
    ("Can you show me examples of past client results?", True, "case_studies"),

    # --- booking_policy.txt ---
    ("How far in advance can I book a call?", True, "booking_policy"),
    ("Can I book a discovery call for right now?", True, "booking_policy"),
    ("What happens if I miss my scheduled call?", True, "booking_policy"),
    ("Can I book two calls at once?", True, "booking_policy"),

    # --- cancellation_policy.txt ---
    ("How do I cancel my discovery call?", True, "cancellation_policy"),
    ("Can I cancel a booking that already happened?", True, "cancellation_policy"),
    ("What's the notice period to cancel my retainer?", True, "cancellation_policy"),

    # --- terms_of_service.txt ---
    ("Who owns the website after the project is done?", True, "terms"),
    ("Do you guarantee specific rankings or results?", True, "terms"),

    # --- privacy_policy.txt ---
    ("What data do you collect through the chatbot?", True, "privacy"),
    ("Can I ask you to delete my information?", True, "privacy"),

    # --- payment_and_billing.txt ---
    ("What payment methods do you accept?", True, "payment"),
    ("What happens if I pay my invoice late?", True, "payment"),

    # --- service_details_seo.txt ---
    ("Does SEO include writing new blog content?", True, "service_seo"),
    ("How long until I see SEO results?", True, "service_seo"),

    # --- service_details_ppc.txt ---
    ("Do you manage Google Ads and Facebook Ads?", True, "service_ppc"),
    ("Is ad spend included in your management fee?", True, "service_ppc"),

    # --- service_details_social.txt ---
    ("How many social media posts do I get per month?", True, "service_social"),
    ("Do you do professional photo shoots for social media?", True, "service_social"),

    # --- service_details_web.txt ---
    ("Does the web design package include ongoing maintenance?", True, "service_web"),
    ("How many revision rounds do I get for my website?", True, "service_web"),

    # --- service_details_branding.txt ---
    ("What's included in the branding package?", True, "service_branding"),
    ("What file formats do I get for my logo?", True, "service_branding"),

    # --- differentiation content (now folded into about_company.txt,
    #     objection_handling.txt was removed — see conversation history) ---
    ("Why should I hire an agency instead of doing it myself?", True, "differentiation"),
    ("What if your marketing doesn't work for my business?", True, "differentiation"),
    ("How are you different from a freelancer?", True, "differentiation"),

    # --- glossary.txt ---
    ("What does ROAS mean?", True, "glossary"),
    ("Can you explain what CTR is?", True, "glossary"),
    ("What's a landing page?", True, "glossary"),

    # --- industries_and_specializations.txt ---
    ("Do you work with restaurants?", True, "industries"),
    ("Do you have experience with healthcare clients?", True, "industries"),
    ("Do you work with pharmaceutical companies?", True, "industries"),

    # --- Off-topic / should fall back ---
    ("Do you sell used cars?", False, "off_topic"),
    ("What's the weather today?", False, "off_topic"),
    ("Can I get a loan?", False, "off_topic"),
    ("How do I cook rice?", False, "off_topic"),
    ("What is the capital of France?", False, "off_topic"),
    ("Who won the last World Cup?", False, "off_topic"),
    ("Can you write me a Python script?", False, "off_topic"),
    ("What's the best smartphone to buy right now?", False, "off_topic"),

    # --- Adjacent-domain traps: sounds business-y but isn't about THIS agency ---
    ("What's the best CRM software for small businesses?", False, "adjacent_trap"),
    ("How do I set up a Facebook Business account myself?", False, "adjacent_trap"),
    ("What's a good hashtag strategy for my own Instagram?", False, "adjacent_trap"),

    # --- Mixed in-domain + out-of-domain in one query ---
    ("Can you also help me file my taxes along with SEO?", True, "mixed"),

    # --- Edge cases: short, empty, gibberish ---
    ("pricing", True, "edge_short"),
    ("hours", True, "edge_short"),
    ("", False, "edge_empty"),
    ("asdkjqwe zxcv 12345 !!!", False, "edge_gibberish"),
]


def run_case(collection, query, expect_in_domain, category, top_k, verbose):
    # Raw distances, shown for debugging only — NOT used for the pass/fail
    # decision, since this bypasses the guards in retrieve() (empty-query
    # rejection, self-service heuristic) that production code actually runs.
    raw = collection.query(query_texts=[query if query.strip() else " "], n_results=top_k)
    docs = raw.get("documents", [[]])[0]
    metas = raw.get("metadatas", [[]])[0]
    dists = raw.get("distances", [[]])[0]

    # The actual decision: run it through the real retrieve() function, so
    # the empty-query guard and self-service heuristic are exercised exactly
    # as they'd run in production, not reimplemented here.
    hits = retrieve(query, top_k=top_k)
    would_answer = len(hits) > 0
    best_dist = min(dists) if dists else float("inf")
    correct = would_answer == expect_in_domain

    status = "PASS" if correct else "FAIL"
    print(f"\n[{status}] ({category}) {query!r}")
    print(f"  expected_in_domain={expect_in_domain}  would_answer={would_answer}  "
          f"raw_best_distance={best_dist:.4f}  threshold={MAX_DISTANCE_THRESHOLD}")

    for doc, meta, dist in zip(docs, metas, dists):
        marker = "<=" if dist <= MAX_DISTANCE_THRESHOLD else " >"
        line = f"    dist={dist:.4f} {marker} thresh  source={meta.get('source')}"
        if verbose:
            line += f"  text={doc[:60]!r}"
        print(line)

    return correct


def analyze_topk_impact(cases, candidate_k=3, baseline_k=TOP_K, verbose=False):
    """For every in-domain test query, compare retrieval at top_k=baseline_k
    (normally 4) vs top_k=candidate_k (normally 3).

    This directly answers "would lowering TOP_K lose anything?" instead of
    guessing. Two things matter, and they're different questions:
      - chunk count: does trimming top_k simply return fewer chunks that
        were passing the distance filter anyway (harmless — those chunks
        were marginal)?
      - distinct sources: does trimming top_k drop a chunk that was the
        ONLY one from its source file (this is the case that can actually
        hurt an answer, since it removes a distinct piece of information
        rather than a near-duplicate of one already included)?

    Note: this only makes sense for in-domain queries — off-topic queries
    should return zero hits at either top_k, so they're skipped here.
    """
    in_domain_cases = [(q, cat) for q, expect, cat in cases if expect]

    rows = []
    for query, category in in_domain_cases:
        hits_base = retrieve(query, top_k=baseline_k)
        hits_small = retrieve(query, top_k=candidate_k)

        sources_base = [h["source"] for h in hits_base]
        sources_small = {h["source"] for h in hits_small}
        # Sources present in the baseline but not reachable at the smaller
        # top_k at all (i.e. genuinely lost, not just re-ranked).
        dropped_sources = [s for s in sources_base if s not in sources_small]
        # dedupe while preserving order
        dropped_sources = list(dict.fromkeys(dropped_sources))

        rows.append({
            "query": query,
            "category": category,
            "chunks_base": len(hits_base),
            "chunks_small": len(hits_small),
            "distinct_sources_base": len(set(sources_base)),
            "distinct_sources_small": len(sources_small),
            "dropped_sources": dropped_sources,
        })

        if verbose:
            print(f"\n({category}) {query!r}")
            print(f"  top_k={baseline_k}: {len(hits_base)} chunks, "
                  f"{len(set(sources_base))} distinct sources -> {sorted(set(sources_base))}")
            print(f"  top_k={candidate_k}: {len(hits_small)} chunks, "
                  f"{len(sources_small)} distinct sources -> {sorted(sources_small)}")
            if dropped_sources:
                print(f"  ⚠ would lose content from: {dropped_sources}")

    queries_losing_a_source = [r for r in rows if r["dropped_sources"]]
    queries_using_4th_chunk = [r for r in rows if r["chunks_base"] >= baseline_k]

    print("\n" + "=" * 60)
    print(f"TOP_K IMPACT: baseline={baseline_k} vs candidate={candidate_k}")
    print("=" * 60)
    print(f"In-domain queries analyzed: {len(rows)}")
    print(f"Queries where the {baseline_k}th chunk cleared the threshold "
          f"(i.e. top_k={baseline_k} is actually being used): "
          f"{len(queries_using_4th_chunk)}/{len(rows)}")
    print(f"Queries that would LOSE a distinct source at top_k={candidate_k}: "
          f"{len(queries_losing_a_source)}/{len(rows)}")

    if queries_losing_a_source:
        print(f"\nQueries where trimming to top_k={candidate_k} drops a distinct source:")
        for r in queries_losing_a_source:
            print(f"  [{r['category']}] {r['query']!r} -> loses {r['dropped_sources']}")
        print(f"\nRecommendation: keep top_k={baseline_k}. These queries pull "
              f"genuinely different source files into context; trimming would "
              f"remove information, not just redundancy.")
    else:
        print(f"\nNo in-domain query lost a distinct source when trimmed to "
              f"top_k={candidate_k}. Any chunks beyond position {candidate_k} "
              f"in these results were from a source already represented "
              f"earlier in the ranking (i.e. redundant, not new information).")
        print(f"Recommendation: top_k={candidate_k} is likely safe to try, "
              f"assuming generation quality with fewer chunks is also fine "
              f"(this script only measures retrieval, not answer quality).")
    print("=" * 60)

    return rows


def main():
    parser = argparse.ArgumentParser(description="Retrieval regression tests for rag_engine.")
    parser.add_argument("--verbose", action="store_true", help="Show matched chunk text.")
    parser.add_argument("--category", default=None,
                         help="Only run queries tagged with this category "
                              "(e.g. booking_policy, off_topic, edge_short).")
    parser.add_argument("--skip-topk-analysis", action="store_true",
                         help="Skip the TOP_K=3 vs TOP_K=4 comparison section.")
    parser.add_argument("--candidate-topk", type=int, default=3,
                         help="The smaller TOP_K value to compare against the "
                              "current TOP_K setting (default: 3).")
    args = parser.parse_args()

    collection = get_chroma_collection()
    print("Total chunks in store:", collection.count())
    if collection.count() == 0:
        print("Knowledge base is empty — run `python rag_engine.py ingest` first.")
        return

    cases = TEST_CASES
    if args.category:
        cases = [c for c in cases if c[2] == args.category]
        if not cases:
            print(f"No test cases found for category {args.category!r}.")
            return

    results = []
    for query, expect_in_domain, category in cases:
        correct = run_case(collection, query, expect_in_domain, category, TOP_K, args.verbose)
        results.append((correct, query, category))

    total = len(results)
    passed = sum(1 for correct, _, _ in results if correct)
    failed = [(query, category) for correct, query, category in results if not correct]

    print("\n" + "=" * 60)
    print(f"SUMMARY: {passed}/{total} passed  ({total - passed} failed)")
    if failed:
        print("\nFailed cases:")
        for query, category in failed:
            print(f"  [{category}] {query!r}")
        print("\nIf failures are in-domain queries with distance just above "
              "threshold, consider raising MAX_DISTANCE_THRESHOLD slightly.")
        print("If failures are off-topic/adjacent-trap queries clearing the "
              "threshold, consider lowering it, or check for KB chunks that "
              "are too generic and match unrelated queries.")
    print("=" * 60)

    if not args.skip_topk_analysis:
        analyze_topk_impact(cases, candidate_k=args.candidate_topk,
                             baseline_k=TOP_K, verbose=args.verbose)


if __name__ == "__main__":
    main()