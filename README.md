<div align="center">

# Agent Memory Leaderboard

**Open, unified, and reproducible evaluation for long-term memory systems.**

[Leaderboard](https://agentmemories.ai/leaderboard/industry/textual) | [2026 Challenge](https://agentmemories.ai/competition/) | [Submit a System](https://agentmemories.ai/evaluation) | [Documentation](https://agentmemories.ai/docs) | [API Guide](https://agentmemories.ai/api-guide)

**English** | [简体中文](README_CN.md)

</div>

Agent Memory Leaderboard (AML) is an open evaluation platform for measuring how
well memory systems store, retrieve, and support the use of long-term
information. It gives research methods, open-source projects, and commercial
memory services a shared protocol, a versioned evaluation process, and a public
leaderboard.

AML was launched on July 29, 2026 by researchers from more than twenty
universities and research organizations. The project brings public benchmarks,
held-out evaluation data, standardized APIs, controlled answer generation,
reviewed scoring, and leaderboard governance into one continuously operated
evaluation program.

> **Agent Memory Challenge 2026 is underway.** The inaugural challenge accepts
> open-source and commercial memory systems. See the
> [official competition page](https://agentmemories.ai/competition/) for the
> current schedule, eligibility requirements, and awards.

## Why AML

Memory systems are often reported on different datasets, with different answer
models, retrieval settings, judges, and aggregation rules. Scores produced
under those conditions are useful within an individual study, but they do not
always support direct comparison across systems.

AML is designed around three goals:

1. **Broad coverage.** Evaluate memory across multiple datasets, context
   lengths, interaction patterns, and application domains.
2. **Controlled comparison.** Keep the answer and evaluation pipeline fixed so
   that score differences are more attributable to the memory system itself.
3. **Actionable diagnosis.** Report capability-level profiles in addition to an
   overall score, making strengths and failure modes easier to understand.

## Evaluation at a glance

| Track | What it evaluates | Current coverage |
| --- | --- | --- |
| **Textual Memory** | Long conversations, cross-session history, personal preferences, rules, temporal events, long contexts, and continuous narratives | More than 10 benchmark datasets, over 1,500 histories and tasks, and nearly 5,000 evaluation questions |
| **Coding Memory** | Whether an agent can retrieve, filter, and reuse relevant engineering experience from earlier work in the same repository | 12 repositories, 150 base software-engineering tasks, and 1,290 time-constrained historical tasks with fine-grained relevance annotations |

The textual suite includes benchmarks such as PersonaMem, LoCoMo-Refined,
CLBench, BEAM, LongMemEval, and ScriptMem. The coding dataset is currently
operated as an unreleased evaluation track; private tasks, verifier materials,
and protected annotations are not distributed through this repository.

Coverage figures describe the current challenge suite and may evolve through
versioned leaderboard releases.

## What AML measures

AML maps questions from different source datasets into a common capability
taxonomy. This makes results comparable at the level of memory behavior rather
than only at the level of dataset names.

### Textual memory capabilities

| Capability | Core question |
| --- | --- |
| Explicit fact recall | Can the system retrieve the right stated facts? |
| Relational and multi-hop reasoning | Can it connect evidence distributed across memories? |
| Temporal and event understanding | Can it distinguish order, updates, and the latest valid state? |
| Memory governance | Can it update, retain, and use memory appropriately over time? |
| Personalization and care | Can it preserve preferences, identity, and user-specific context? |
| Rules and process execution | Can it recall and follow established constraints and procedures? |
| Epistemic safety and privacy | Can it respect evidence boundaries, uncertainty, and sensitive information? |

### Coding memory capabilities

- **Debug Memory:** reuse earlier debugging signals, repair strategies, and
  validation experience when solving a related issue.
- **Development Memory:** recover architectural decisions and established
  engineering patterns when implementing new behavior.

## A controlled evaluation protocol

Participants expose only two memory operations:

- **Add** writes a conversation, event, document, or engineering history into
  the memory system.
- **Search** returns relevant memory evidence for a supplied query and scope.

The platform controls answer generation, evaluation, aggregation, and run
orchestration. A versioned evaluation contract records the benchmark bundle,
pipeline revision, model configuration, and scoring rules used for every
comparable result.

This separation matters: the participant is responsible for memory, while AML
holds the downstream measurement conditions constant.

### Evaluation flow

```text
Apply for an AML Key
        |
Provide Add and Search APIs
        |
Pass the compatibility smoke test
        |
Run the complete evaluation suite
        |
Review and publish the result
```

All formal results remain private until the evaluation completes and the
submission passes leaderboard review. Public entries are tied to a named
system version and its evaluation contract so that later updates remain
traceable.

## Participate

AML supports two leaderboard categories:

| Category | Intended for | Submission expectation |
| --- | --- | --- |
| **Open-source methods** | Research teams and open-source maintainers | Public code, configuration, attribution, and reproducibility materials |
| **Commercial products** | Hosted memory products and API providers | A stable Add/Search service; internal implementation may remain private |

To submit a system:

1. Read the [rules](https://agentmemories.ai/rules) and
   [API guide](https://agentmemories.ai/api-guide).
2. Implement publicly reachable Add and Search endpoints.
3. Submit an [evaluation access request](https://agentmemories.ai/evaluation).
4. Use the issued AML Key to run the non-scored compatibility smoke test.
5. Start a full evaluation and monitor the private result.
6. Request review for publication on the appropriate leaderboard.

Participants operate and fund their own memory API, storage, bandwidth, and
compute. AML operates the answer, evaluation, orchestration, and leaderboard
infrastructure.

## Repository scope

This repository is the public evaluation release for AML. It is not a copy of
the production leaderboard service and it does not contain benchmark data.

```text
agent-memory-leaderboard/
|-- data/               # Public per-benchmark evaluation contracts
|   |-- beam/
|   |-- clbench/
|   |-- locomo-refined/
|   |-- longmemeval-s/
|   |-- personamem/
|   `-- scriptmem/
|-- api_config.py       # Shared public runtime configuration surface
|-- requirements.txt    # Minimal Python dependency set
|-- README.md           # English
`-- README_CN.md        # Simplified Chinese
```

The published modules make the public answer and scoring behavior inspectable.
They are provided for transparency, methodological review, and alignment with
reported leaderboard results.

### Deliberately not included

To protect benchmark integrity and participant privacy, this repository does
not publish:

- benchmark corpora, held-out questions, gold answers, rubrics, or private
  annotations;
- production databases, participant submissions, retrieved memories, model
  outputs, logs, or run artifacts;
- service credentials, provider keys, participant API keys, or deployment
  secrets;
- internal orchestration, administration, infrastructure, or review tooling;
- unreleased benchmark implementations or verifier materials.

Do not submit any of these materials in an issue or pull request.

## Reproducibility and integrity

- Public leaderboard comparisons are valid only when the complete evaluation
  contract matches, including the benchmark release and pipeline version.
- Credentials and service endpoints must be supplied externally; no secret is
  bundled with this repository.
- Published scores use a 0-100 scale. Dataset-native metrics may use different
  internal ranges before leaderboard normalization.
- Source datasets and adapted evaluators remain subject to their respective
  upstream licenses and usage terms.
- A result is published only after a successful full run and review. Smoke,
  partial, failed, and incomplete runs are not leaderboard entries.

## Agent Memory Challenge 2026

The first Agent Memory Challenge opened on July 29, 2026. It is organized by
the Agent Memory Leaderboard platform for researchers, open-source maintainers,
and commercial memory teams worldwide.

| Milestone | Date |
| --- | --- |
| Registration opened | July 29, 2026 |
| Submission deadline | August 7, 2026 |
| First results | Mid-August 2026 |

The challenge provides separate rankings for open-source methods and commercial
products. Membership and API-credit awards, contribution rewards, and any
deadline updates are governed by the
[official challenge rules](https://agentmemories.ai/competition/).

AML continues to operate after the inaugural challenge. New systems, updated
versions, and complementary benchmark proposals can be evaluated through later
leaderboard releases.

## Contributing

We welcome contributions that improve public documentation, clarify an exposed
evaluation contract, or propose a complementary benchmark with meaningful
difficulty and clear provenance.

Before opening a contribution:

1. Do not include private evaluation traces, participant data, credentials, or
   proprietary system details.
2. Preserve upstream attribution and licensing information.
3. Explain whether a change affects scoring or only documentation.
4. Treat any scoring change as a new evaluation-contract version rather than a
   silent modification of existing results.

For benchmark proposals and current contribution requirements, see the
[documentation](https://agentmemories.ai/docs).

## Citation

If AML is useful in your research or evaluation work, please cite the project:

```bibtex
@misc{agent_memory_leaderboard_2026,
  title        = {Agent Memory Leaderboard: Open and Reproducible Evaluation for Long-Term Memory Systems},
  author       = {{Agent Memory Leaderboard Organizers}},
  year         = {2026},
  howpublished = {\url{https://agentmemories.ai/}},
  note         = {Accessed: YYYY-MM-DD}
}
```

## Links and contact

- Website: [agentmemories.ai](https://agentmemories.ai/)
- Public leaderboard: [Textual Memory Leaderboard](https://agentmemories.ai/leaderboard/industry/textual)
- Challenge: [Agent Memory Challenge 2026](https://agentmemories.ai/competition/)
- Submission: [Evaluation Access](https://agentmemories.ai/evaluation)
- Documentation: [Platform Documentation](https://agentmemories.ai/docs)
- API guide: [Add/Search Integration Guide](https://agentmemories.ai/api-guide)


---

Memory systems need a measurement standard that is broad enough to be useful,
controlled enough to be comparable, and transparent enough to be trusted. AML
is built to make that standard a shared, evolving piece of infrastructure.
