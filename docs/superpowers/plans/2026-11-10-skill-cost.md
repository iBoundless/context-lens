# Skill Cost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show per-skill estimates for a selected request and accumulated across all retained session requests.

**Architecture:** Split recognized skill text into disjoint analysis blocks. Persist versioned per-request skill summaries and aggregate their metadata server-side for the existing overview API. Render a joined selected-request/session table without loading session bodies into the browser.

**Tech Stack:** Python, unittest, SQLite, native JavaScript, Node test runner. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-10-skill-cost-design.md`

## Global Constraints

- Estimate UTF-8 bytes / 4; do not label estimates as provider usage or monetary cost.
- Explicit source metadata takes priority over recognition of standard tags.
- Separate descriptions from loaded content, never count the same text twice.
- Include all retained physical requests, including auxiliary/repeated attempts; exclude response and gap events from sums.
- Preserve raw events, provider usage, consent and authentication. Do not send model requests.
- Hidden server context and unrecognized content remain unknown, not zero.
- Use only hyphen-minus for dashes in new copy and code comments.

## Review Focus

- Nested, unclosed or quoted skill tags must not crash or imply proven activation.
- Multimodal messages with skill text must retain unknown-size warnings for attachments.
- Identical names in different clients/sessions must not leak totals across sessions.
- Stale index upgrade must preserve late usage, be repeatable and not change raw captures.
- Session switches and live refresh must not show a previous session's skill statistics.

## File Structure

- Create `context_lens/skills.py`: conservative text segmentation and skill aggregation helpers.
- Modify `context_lens/analysis.py`: integrate segmentation while preserving protocol and attachment blocks.
- Modify `context_lens/store.py`: versioned analysis rebuild and session skill metadata aggregation.
- Modify `web/view-model.mjs`: pure selected/session row joining.
- Modify `web/app.js`, `web/index.html`, `web/style.css`: table, provenance and coverage notices.
- Create `tests/test_skills.py`; extend `tests/test_core.py`, `tests/test_server.py`, `tests/view-model.test.mjs`.
- Modify `README.md`: counting semantics and limitations.

### Task 1: Disjoint skill attribution

**Interfaces:** `analyze(payload)` adds `skills`, a list of `{name, kind, estimated_tokens, occurrences, attributions}`; kind is `loaded` or `description`. Skill blocks add `skill_name` and `skill_kind`, retain category `skills`. Attribution is `source-metadata` or `format-heuristic`.

- [ ] Add `tests/test_skills.py` cases: two standard `<skill_content name="...">` spans with surrounding text; explicit `skill:review` containing a differently named tag; repeated same-name blocks; malformed/non-string names; unclosed/nested tags; tagged text in user/tool messages and standard content arrays.
- [ ] Assert the explicit source owns the entire marked span; raw text segments concatenate to the original text; no skill substring also remains in its parent block; sums equal the sum of disjoint blocks, with existing rounding semantics.
- [ ] Add catalog cases for complete `<available_skills><skill><name>...</name><description>...</description></skill></available_skills>` only. Catalog entries produce `description`, never `loaded`; ambiguous/incomplete entries stay unattributed. Existing `<skill><id>...` fields may be retained in the entry span but names come from `name`.
- [ ] Add attachment case: text tags alongside image/file content preserve unknown multimodal totals. Assert source metadata wins over all nested format recognition. Format recognition states presence, not activation, including quoted tags.
- [ ] Run `.venv/bin/python -m unittest discover -s tests -p test_skills.py -v`; expect missing skill summaries or assertion failures before implementation.
- [ ] Implement `segment_skill_text(value: str) -> list[dict]` and `summarize_skills(blocks: list[dict]) -> list[dict]` in `context_lens/skills.py`. Segments carry exact content and optional name/kind/attribution; skip ambiguous nesting conservatively. Summary groups by `(name, kind)`, preserves occurrence counts and sorted attribution sets.
- [ ] Integrate in `analysis.py`: split only textual fields of known protocol containers, preserving surrounding serialized envelope/other fields and unknown modality state. For explicit metadata assign skill fields directly without recursively segmenting that span. Do not scan tool schema definitions as loaded skills.
- [ ] Run skill and existing core tests, expect PASS. Commit focused analyzer changes.

### Task 2: Session totals and safe existing-index upgrade

**Interfaces:** selected `/api/wave/{seq}` and wave summaries include `skills`. `Store.overview(client, session)` adds `skill_totals` entries `{name, kind, estimated_tokens, request_count, occurrences, attributions}`, plus `skill_coverage` object `{request_count, gap_count, incomplete_request_count, server_state_unknown_count}`. Absence of identified skills is not proof of no skills.

- [ ] Add Store tests with 105 requests, including auxiliary kinds, repeated skill names, response completions and gaps. Assert request_count counts each request once per `(name, kind)`; occurrences counts repeated spans; totals include requests outside the 100-wave page; gaps/responses never increment skill totals.
- [ ] Add session/client isolation, deletion and prune tests; assert no stale skill totals survive deletion. Add incomplete capture and previous_response_id coverage assertions.
- [ ] Add migration test: create persisted data with old analysis/summary, late usage and consent enabled; reopen Store and assert new skills appear, usage/raw events/recording unchanged; reopen again and assert idempotence.
- [ ] Run `.venv/bin/python -m unittest discover -s tests -p test_core.py -v`; expect failures on missing totals/migration.
- [ ] Add `ANALYSIS_VERSION = 2` to analysis output and version-aware startup rebuild in `store.py`. Reuse `_index_request` only for stale/missing rows within a transaction; usage is reattached from stored responses. Ensure gap summaries have no skills even if a gap payload resembles skill text.
- [ ] Store full skill summary independently of the 500-block preview. Aggregate metadata over all request summaries using streaming SQLite row iteration, not full raw bodies. Coverage counts all retained requests and gaps; sums describe visible text even when server context is unknown.
- [ ] Add authenticated API integration assertions for wave/overview fields and unauthorized denial to `tests/test_server.py`; run full Python suite, expect PASS. Commit storage/API changes.

### Task 3: Selected-request/session table and documentation

**Interfaces:** export `skillRows(current, overview)` from `web/view-model.mjs`, returning `{name, kind, selected_tokens, selected_present, percent, session_tokens, request_count, attributions}` rows, joined by `(name, kind)`. Percentage uses selected estimated_tokens; unknown denominator yields null. Not present differs from measured zero.

- [ ] Add Node tests joining selected and session data: session-only names, loaded versus description, repeated names grouped upstream, null totals/zero denominator, distinct attribution labels, empty state, and two successive calls for different sessions with no retained state.
- [ ] Run `node --test tests/view-model.test.mjs`; expect missing export/assertion failures.
- [ ] Implement `skillRows` as a pure function; sort by descending session estimate with name/kind tie breakers. Render session-only selected values as absence, not a claim of zero skill cost.
- [ ] Add `skills` section outside the current-wave visual panel, so session totals remain visible with no selected wave. Render via textContent using the existing element helper; show name, loaded/description kind, selected estimate, selected share, session sum, request count and provenance. No HTML interpretation of names.
- [ ] Invoke rendering on empty-state and normal render paths. Use latest overview skill metadata and selected full-wave/summary data consistently; on session change do not reuse a previous overview or asynchronous selected-wave result. Clear/guard in-flight results with session/revision identity.
- [ ] Add persistent notice: estimates include repeats across captured requests, descriptions are not activation, format tags are heuristic, hidden context/gaps cannot be reconstructed. Describe UTF-8 rounding and incomplete coverage in README.
- [ ] Run `node --test tests/*.test.mjs` and `.venv/bin/python -m unittest discover -s tests -v`; expect all PASS. Run `git diff --check`; expect no errors. Commit UI/docs changes.
- [ ] Browser smoke with synthetic data in an isolated temporary state directory/port: login, skill rows, selected-wave switching, two sessions, live update, description separation, unknown context and hostile skill name rendered as text. Do not inject fixtures into the live capture database. Record actual results in the plan.

### Task 4: Delivery verification

- [ ] Review staged/tracked changes against the spec and confirm private runtime artifacts/internal notes remain untracked or ignored. Check working tree for unrelated user edits.
- [ ] Fresh full test run, verify no regressions and record counts. Review migration test output before any live service update; make a private backup of the live database before a restart that rebuilds its index.
- [ ] Report implementation, checks and remaining limitations. Push and restart live service only if authorized for this update; do not treat prior publication or systemd setup as blanket authorization for deployment.

## Self-Review

Spec coverage: per-wave and whole-session data, source/format priority, loaded/catalog separation, no double count, migration, retained-window scope, incomplete coverage and documentation each have an owning task. The five Review Focus cases are assigned to Tasks 1-3. Interface names are shared consistently; no new endpoint or dependency is required.

## Execution Evidence

- Tasks 1-3 implemented with RED/GREEN tests. Before final review: Python44/44, JavaScript16/16, JavaScript syntax check and git diff whitespace check passed.
- Browser smoke on isolated synthetic collector at port18769: login; two-session isolation; selected-wave switching; live auxiliary request increments accumulated tokens/count; separate loaded/catalog entries; unknown server-state notice; hostile name rendered as text, no img element. Expected initial401 before login was the only console error.
- Working systemd collector, live captures and remote main remain untouched by feature development.
- Independent whole-branch review found one Important startup issue for malformed content types. Added list/dict analyzer and legacy-index upgrade regressions, observed RED, applied type guard, verified GREEN: Python46/46, JavaScript16/16. No other reviewer findings or deferred minors.

## Delivery Status

- [x] Task1: disjoint analyzer attribution, tests and commit.
- [x] Task2: versioned old-index upgrade and whole-session totals, tests and commit.
- [x] Task3: table, documentation, automated checks and isolated browser smoke.
- [x] Task4: privacy audit, independent review, regression fix and final local verification.
- Integration, push and live deployment require a separate user decision. No live database migration or restart was performed.
