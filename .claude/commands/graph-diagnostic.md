# /graph-diagnostic — Full Graph Schema & Data Quality Audit

Run a comprehensive diagnostic of the Neo4j graph and write all results to a single file for external analysis.

## Instructions

Run every query below via `bash bin/graph.sh query "..."`. Collect ALL raw output — do not summarize, interpret, or truncate. Write the complete results to `memory/diagnostics/graph-diagnostic-YYYY-MM-DD.md` (use today's date). Each section header and the raw query output must be preserved exactly as returned.

After all queries complete, push the file to memory and report: `[diagnostic] ✓ Written to memory/diagnostics/graph-diagnostic-{date}.md — {N} queries executed`

Do NOT analyze the results. Do NOT propose fixes. Just capture the data.

---

## Setup

```bash
mkdir -p memory/diagnostics
```

Set the output file path:
```bash
DIAG_FILE="memory/diagnostics/graph-diagnostic-$(date +%Y-%m-%d).md"
echo "# Egregore Graph Diagnostic — $(date +%Y-%m-%d)" > "$DIAG_FILE"
echo "" >> "$DIAG_FILE"
echo "Generated at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$DIAG_FILE"
echo "" >> "$DIAG_FILE"
```

---

## Queries

For each section below: append the section header to `$DIAG_FILE`, run the query, append the raw output. Use this pattern:

```bash
echo "## Section Name" >> "$DIAG_FILE"
echo '```' >> "$DIAG_FILE"
bash bin/graph.sh query "CYPHER_HERE" >> "$DIAG_FILE" 2>&1
echo '```' >> "$DIAG_FILE"
echo "" >> "$DIAG_FILE"
```

### 1. Node Labels

```cypher
CALL db.labels() YIELD label RETURN label ORDER BY label
```

### 2. Relationship Types

```cypher
CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType ORDER BY relationshipType
```

### 3. Artifact Property Coverage

```cypher
MATCH (a:Artifact)
WITH count(a) AS total,
  count(a.id) AS has_id,
  count(a.title) AS has_title,
  count(a.type) AS has_type,
  count(a.topics) AS has_topics,
  count(a.filePath) AS has_filePath,
  count(a.created) AS has_created,
  count(a.confidence) AS has_confidence,
  count(a.origin) AS has_origin,
  count(a.analysis) AS has_analysis,
  count(a.embedding) AS has_embedding,
  count(a.status) AS has_status,
  count(a.salience) AS has_salience
RETURN total, has_id, has_title, has_type, has_topics, has_filePath, has_created, has_confidence, has_origin, has_analysis, has_embedding, has_status, has_salience
```

### 4. Session Property Coverage

```cypher
MATCH (s:Session)
WITH count(s) AS total,
  count(s.id) AS has_id,
  count(s.date) AS has_date,
  count(s.topic) AS has_topic,
  count(s.summary) AS has_summary,
  count(s.filePath) AS has_filePath,
  count(s.handoffStatus) AS has_handoffStatus
RETURN total, has_id, has_date, has_topic, has_summary, has_filePath, has_handoffStatus
```

### 5. Quest Property Coverage

```cypher
MATCH (q:Quest)
WITH count(q) AS total,
  count(q.id) AS has_id,
  count(q.title) AS has_title,
  count(q.status) AS has_status,
  count(q.description) AS has_description,
  count(q.topics) AS has_topics
RETURN total, has_id, has_title, has_status, has_description, has_topics
```

### 6. Person Nodes

```cypher
MATCH (p:Person)
RETURN p.name AS name, keys(p) AS properties
```

### 7. Other Node Types (non-standard)

```cypher
MATCH (n)
WHERE NOT n:Artifact AND NOT n:Session AND NOT n:Quest AND NOT n:Person
RETURN labels(n) AS label, count(n) AS count, keys(head(collect(n))) AS sample_properties
```

### 8. Edge Distribution

```cypher
MATCH (a)-[r]->(b)
RETURN type(r) AS edge_type, labels(a)[0] AS from_label, labels(b)[0] AS to_label, count(r) AS count
ORDER BY count DESC
```

### 9. Edge Properties

```cypher
MATCH ()-[r]->()
WHERE size(keys(r)) > 0
RETURN type(r) AS edge_type, keys(r) AS properties, count(r) AS count
ORDER BY count DESC
```

### 10. Edges With No Properties

```cypher
MATCH ()-[r]->()
WHERE size(keys(r)) = 0
RETURN type(r) AS edge_type, count(r) AS count
ORDER BY count DESC
```

### 11. Artifact Type Distribution

```cypher
MATCH (a:Artifact)
RETURN a.type AS type, count(a) AS count
ORDER BY count DESC
```

### 12. Topic Frequency (top 50)

```cypher
MATCH (a:Artifact)
WHERE a.topics IS NOT NULL
UNWIND a.topics AS topic
RETURN topic, count(*) AS frequency
ORDER BY frequency DESC
LIMIT 50
```

### 13. Topic Stats

```cypher
MATCH (a:Artifact)
WHERE a.topics IS NOT NULL
RETURN count(a) AS artifacts_with_topics,
       avg(size(a.topics)) AS avg_topics_per_artifact,
       max(size(a.topics)) AS max_topics,
       min(size(a.topics)) AS min_topics
```

### 14. Total Unique Topics

```cypher
MATCH (a:Artifact)
WHERE a.topics IS NOT NULL
UNWIND a.topics AS topic
WITH DISTINCT topic
RETURN count(topic) AS unique_topics
```

### 15. Artifact Connectivity

```cypher
MATCH (a:Artifact)
OPTIONAL MATCH (a)-[r]-()
WITH a, count(r) AS degree
RETURN min(degree) AS min_degree, max(degree) AS max_degree, avg(degree) AS avg_degree,
       count(CASE WHEN degree = 0 THEN 1 END) AS isolated_nodes,
       count(CASE WHEN degree = 1 THEN 1 END) AS single_edge,
       count(CASE WHEN degree >= 3 THEN 1 END) AS well_connected,
       count(a) AS total
```

### 16. Session Connectivity

```cypher
MATCH (s:Session)
OPTIONAL MATCH (s)-[r]-()
WITH s, count(r) AS degree
RETURN min(degree) AS min_degree, max(degree) AS max_degree, avg(degree) AS avg_degree,
       count(CASE WHEN degree = 0 THEN 1 END) AS isolated_sessions,
       count(s) AS total
```

### 17. Temporal Distribution (artifacts per day)

```cypher
MATCH (a:Artifact)
WHERE a.created IS NOT NULL
RETURN substring(toString(a.created), 0, 10) AS day, count(*) AS artifacts_created
ORDER BY day
```

### 18. Quest Coverage

```cypher
MATCH (q:Quest)
OPTIONAL MATCH (a:Artifact)-[:PART_OF]->(q)
WITH q, count(a) AS artifact_count
OPTIONAL MATCH (s:Session)-[:ADVANCED]->(q)
RETURN q.id AS quest, q.status AS status, artifact_count,
       count(s) AS session_count
ORDER BY artifact_count DESC
```

### 19. Richest Artifacts (most properties)

```cypher
MATCH (a:Artifact)
WITH a, size(keys(a)) AS property_count
ORDER BY property_count DESC LIMIT 10
RETURN a {.*} AS artifact, property_count, keys(a) AS properties
```

### 20. Poorest Artifacts (fewest properties)

```cypher
MATCH (a:Artifact)
WITH a, size(keys(a)) AS property_count
ORDER BY property_count ASC LIMIT 10
RETURN a {.*} AS artifact, property_count, keys(a) AS properties
```

### 21. Ghost Artifacts (full census)

```cypher
MATCH (a:Artifact)
WHERE a.filePath IS NULL
OPTIONAL MATCH (a)-[r]-()
WITH a, count(r) AS degree, collect(type(r)) AS edge_types
RETURN a.id AS id, a.title AS title, a.type AS type, a.topics AS topics, a.created AS created, degree, edge_types, keys(a) AS all_properties
ORDER BY a.created DESC
```

### 22. Path Lengths (how deep does the graph go)

```cypher
MATCH path = (a:Artifact)-[*1..4]-(b:Artifact)
WHERE a <> b
RETURN length(path) AS path_length, count(*) AS occurrences
ORDER BY path_length DESC
```

### 23. Timestamp Format Audit

```cypher
MATCH (a:Artifact)
WHERE a.created IS NOT NULL
WITH a,
  CASE
    WHEN toString(a.created) CONTAINS 'T' THEN 'datetime'
    WHEN toString(a.created) =~ '\\d{4}-\\d{2}-\\d{2}' THEN 'date'
    ELSE 'other'
  END AS format_type
RETURN format_type, count(a) AS count, head(collect(toString(a.created))) AS example
ORDER BY count DESC
```

### 24. Origin Distribution (if populated)

```cypher
MATCH (a:Artifact)
WHERE a.origin IS NOT NULL
RETURN a.origin AS origin, count(a) AS count
ORDER BY count DESC
```

### 25. Cross-Quest Artifacts (artifacts in multiple quests)

```cypher
MATCH (a:Artifact)-[:PART_OF]->(q:Quest)
WITH a, collect(q.id) AS quests, count(q) AS quest_count
WHERE quest_count > 1
RETURN a.id AS id, a.title AS title, quests
```

### 26. Orphan Quests (quests with no artifacts and no sessions)

```cypher
MATCH (q:Quest)
WHERE NOT (q)<-[:PART_OF]-() AND NOT (q)<-[:ADVANCED]-()
RETURN q.id AS quest, q.title AS title, q.status AS status
```

---

## Finalize

After all queries:

```bash
# Count sections
SECTION_COUNT=$(grep -c '^## ' "$DIAG_FILE")
echo "" >> "$DIAG_FILE"
echo "---" >> "$DIAG_FILE"
echo "Total sections: $SECTION_COUNT" >> "$DIAG_FILE"
```

Commit and push:

```bash
cd memory && git add -A && git commit -m "Add graph diagnostic $(date +%Y-%m-%d)" && git push origin main
```

Report: `[diagnostic] ✓ Written to memory/diagnostics/graph-diagnostic-{date}.md — {N} queries executed`
