# Memory Governance

## Design Goal

Memory governance keeps long-term memory useful over time. It decays stale low-value memories, detects conflicting facts, merges duplicates, and produces audit reports for cleanup.

## Key Decisions

- Forgetting uses importance decay plus access-frequency compensation.
- Conflict detection groups memories by a simple subject prefix, which is deterministic and easy to test.
- Merge keeps the highest-importance memory for each subject.

## Interview Talking Points

- Long-term memory without governance becomes noisy and eventually hurts retrieval quality.
- The first version is deterministic and explainable; later versions can use embeddings or LLM-based conflict detection.
- Governance is separate from storage, so it works for in-memory and Milvus-backed memories.