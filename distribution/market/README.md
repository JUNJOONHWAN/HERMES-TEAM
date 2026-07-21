# Optional market memory

HERMES-TEAM ships no private know-how. Operators can create an empty,
local JSONL store and add only lessons they own:

```bash
python3 scripts/market_memory.py \
  --db ~/.hermes/knowledge/market_memory.jsonl init

python3 scripts/market_memory.py \
  --db ~/.hermes/knowledge/market_memory.jsonl add \
  --title 'Earnings timestamp rule' \
  --body 'Record whether the source timestamp is pre-market or after-hours.' \
  --tag earnings \
  --source https://example.com/source

python3 scripts/market_memory.py \
  --db ~/.hermes/knowledge/market_memory.jsonl search 'earnings timing'
```

This store is advisory memory, not an authority and not a capability grant. The
market Role Shell still requires current public-source verification. Memory
writes require an explicit operator/card instruction; normal research is
read-only. Entries used in an answer are cited by their `mm_...` ID.
