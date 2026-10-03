"""Scoped data access. Every query an endpoint runs goes through here.

Isolation rules, enforced in one place:
  - Every query is filtered by the caller's tenant_id.
  - Matchmakers only see their own clients and the shares made for them.
  - Leads and admins see everything inside their own tenant, never another tenant.
  - Out-of-scope records return None (the API answers 404, not 403),
    so a caller cannot even learn that a record exists.

In production the same rules are also enforced by PostgreSQL row-level security,
so a bug here still cannot leak data across tenants.
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from app import matching
from app.db import Principal
from app.security import decrypt, encrypt

CANDIDATE_PUBLIC_FIELDS = (
    "id, name, gender, age, city, religion, diet, smoking, drinking, wants_children, "
    "open_to_relocate, height_cm, education, profession"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScopedRepo:
    def __init__(self, conn: sqlite3.Connection, principal: Principal):
        self.conn = conn
        self.p = principal

    # ------------------------------------------------------------ scope helpers

    def _client_scope(self, alias: str = "c") -> tuple[str, list]:
        sql = f"{alias}.tenant_id = ?"
        params: list = [self.p.tenant_id]
        if self.p.role == "matchmaker":
            sql += f" AND {alias}.matchmaker_id = ?"
            params.append(self.p.user_id)
        return sql, params

    # ------------------------------------------------------------ clients

    def list_clients(self) -> list[dict]:
        scope, params = self._client_scope()
        rows = self.conn.execute(
            f"""SELECT c.id, c.name, c.gender, c.age, c.city, u.name AS matchmaker_name,
                       (SELECT COUNT(*) FROM shares s WHERE s.client_id = c.id AND s.tenant_id = c.tenant_id) AS shares
                FROM clients c JOIN users u ON u.id = c.matchmaker_id
                WHERE {scope} ORDER BY c.name""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_client(self, client_id: str) -> dict | None:
        scope, params = self._client_scope()
        row = self.conn.execute(
            f"""SELECT c.*, u.name AS matchmaker_name FROM clients c
                JOIN users u ON u.id = c.matchmaker_id
                WHERE c.id = ? AND {scope}""",
            [client_id, *params],
        ).fetchone()
        if row is None:
            return None
        client = dict(row)
        client["notes"] = decrypt(client.pop("notes_enc"))
        client["preference_card"] = self._card(client_id)
        return client

    def _card(self, client_id: str) -> dict:
        row = self.conn.execute(
            "SELECT hard_json, soft_json, version, updated_at FROM preference_cards WHERE client_id = ? AND tenant_id = ?",
            (client_id, self.p.tenant_id),
        ).fetchone()
        return {
            "hard": json.loads(row["hard_json"]),
            "soft": json.loads(row["soft_json"]),
            "version": row["version"],
            "updated_at": row["updated_at"],
        }

    def update_card(self, client_id: str, hard: dict, soft: list[dict]) -> dict | None:
        if self.get_client(client_id) is None:
            return None
        self.conn.execute(
            """UPDATE preference_cards SET hard_json = ?, soft_json = ?, version = version + 1, updated_at = ?
               WHERE client_id = ? AND tenant_id = ?""",
            (json.dumps(hard), json.dumps(soft), now_iso(), client_id, self.p.tenant_id),
        )
        self.audit("update_preference_card", "client", client_id)
        self.conn.commit()
        return self._card(client_id)

    # ------------------------------------------------------------ candidates

    def get_candidate(self, candidate_id: str) -> dict | None:
        row = self.conn.execute(
            f"SELECT {CANDIDATE_PUBLIC_FIELDS} FROM candidates WHERE id = ? AND tenant_id = ?",
            (candidate_id, self.p.tenant_id),
        ).fetchone()
        return dict(row) if row else None

    def candidate_pool_size(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE tenant_id = ?", (self.p.tenant_id,)
        ).fetchone()[0]

    # ------------------------------------------------------------ matching

    def check(self, client_id: str, candidate_id: str) -> dict | None:
        client = self.get_client(client_id)
        candidate = self.get_candidate(candidate_id)
        if client is None or candidate is None:
            return None
        result = matching.check(client["preference_card"], candidate)
        return {"candidate": candidate, **result.to_dict()}

    def shortlist(self, client_id: str, limit: int) -> dict | None:
        client = self.get_client(client_id)
        if client is None:
            return None
        card = client["preference_card"]
        started = time.perf_counter()

        where, params = matching.sql_prefilter(card["hard"])
        rows = self.conn.execute(
            f"""SELECT {CANDIDATE_PUBLIC_FIELDS} FROM candidates
                WHERE tenant_id = ? AND {where}
                  AND id NOT IN (SELECT candidate_id FROM shares WHERE client_id = ? AND tenant_id = ?)""",
            [self.p.tenant_id, *params, client_id, self.p.tenant_id],
        ).fetchall()
        sql_ms = (time.perf_counter() - started) * 1000

        scored = []
        for row in rows:
            cand = dict(row)
            result = matching.check(card, cand)
            if result.status != "BLOCK":  # defensive: the SQL prefilter should already exclude these
                scored.append({"candidate": cand, **result.to_dict()})
        scored.sort(key=lambda r: (-r["fit_score"], r["candidate"]["id"]))
        total_ms = (time.perf_counter() - started) * 1000

        return {
            "data": scored[:limit],
            "meta": {
                "pool_size": self.candidate_pool_size(),
                "passed_hard_filters": len(rows),
                "card_version": card["version"],
                "sql_ms": round(sql_ms, 1),
                "compute_ms": round(total_ms, 1),
            },
        }

    # ------------------------------------------------------------ shares

    def create_share(self, client_id: str, candidate_id: str, override_reason: str | None) -> dict | None:
        """Re-checks on the server. A blocked candidate can only be shared with a written reason."""
        checked = self.check(client_id, candidate_id)
        if checked is None:
            return None
        if checked["status"] == "BLOCK" and not override_reason:
            return {"blocked": True, **checked}
        client = self.get_client(client_id)
        share_id = uuid.uuid4().hex
        self.conn.execute(
            """INSERT INTO shares (id, tenant_id, client_id, candidate_id, matchmaker_id, check_status,
                                   fit_score, override_reason, shared_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (share_id, self.p.tenant_id, client_id, candidate_id, client["matchmaker_id"],
             checked["status"], checked["fit_score"], override_reason, now_iso()),
        )
        if override_reason:
            self.audit("override_block", "share", share_id, override_reason)
        self.audit("share_profile", "share", share_id)
        self.conn.commit()
        return {"blocked": False, "share_id": share_id, **checked}

    def _share_row(self, share_id: str):
        scope, params = self._client_scope("c")
        return self.conn.execute(
            f"""SELECT s.*, c.name AS client_name FROM shares s JOIN clients c ON c.id = s.client_id
                WHERE s.id = ? AND s.tenant_id = ? AND {scope}""",
            [share_id, self.p.tenant_id, *params],
        ).fetchone()

    def list_shares(self, client_id: str) -> list[dict] | None:
        if self.get_client(client_id) is None:
            return None
        rows = self.conn.execute(
            """SELECT s.id, s.candidate_id, k.name AS candidate_name, s.outcome, s.check_status, s.fit_score,
                      s.override_reason, s.is_historical, s.shared_at,
                      r.id AS rejection_id, r.status AS rejection_status, r.reason_code, r.reason_detail
               FROM shares s JOIN candidates k ON k.id = s.candidate_id
               LEFT JOIN rejections r ON r.share_id = s.id AND r.tenant_id = s.tenant_id
               WHERE s.client_id = ? AND s.tenant_id = ?
               ORDER BY s.is_historical, s.shared_at DESC LIMIT 50""",
            (client_id, self.p.tenant_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def record_outcome(self, share_id: str, outcome: str, feedback: str | None) -> dict | None:
        share = self._share_row(share_id)
        if share is None:
            return None
        if share["outcome"] != "pending":
            return {"error": "Outcome already recorded"}
        self.conn.execute(
            "UPDATE shares SET outcome = ? WHERE id = ? AND tenant_id = ?", (outcome, share_id, self.p.tenant_id)
        )
        rejection_id = None
        if outcome == "rejected":
            rejection_id = uuid.uuid4().hex
            self.conn.execute(
                """INSERT INTO rejections (id, tenant_id, share_id, raw_text_enc, status, created_at)
                   VALUES (?, ?, ?, ?, 'queued', ?)""",
                (rejection_id, self.p.tenant_id, share_id, encrypt(feedback or ""), now_iso()),
            )
        self.audit("record_outcome", "share", share_id, outcome)
        self.conn.commit()
        return {"share_id": share_id, "outcome": outcome, "rejection_id": rejection_id}

    def get_rejection(self, rejection_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM rejections WHERE id = ? AND tenant_id = ?", (rejection_id, self.p.tenant_id)
        ).fetchone()
        if row is None or self._share_row(row["share_id"]) is None:
            return None
        out = {k: row[k] for k in ("id", "share_id", "status", "reason_code", "reason_detail", "confidence", "classifier")}
        out["suggestion"] = json.loads(row["suggestion_json"]) if row["suggestion_json"] else None
        out["text"] = decrypt(row["raw_text_enc"])
        return out

    def reveal_contact(self, share_id: str) -> dict | None:
        """Contact details are hidden until the client accepts. Every reveal is audited."""
        share = self._share_row(share_id)
        if share is None:
            return None
        if share["outcome"] != "accepted":
            return {"error": "Contact details are only available after the client accepts"}
        row = self.conn.execute(
            "SELECT phone, email FROM candidates WHERE id = ? AND tenant_id = ?",
            (share["candidate_id"], self.p.tenant_id),
        ).fetchone()
        self.audit("reveal_contact", "share", share_id)
        self.conn.commit()
        return dict(row)

    # ------------------------------------------------------------ dashboard

    def _share_scope(self) -> tuple[str, list]:
        sql, params = "s.tenant_id = ?", [self.p.tenant_id]
        if self.p.role == "matchmaker":
            sql += " AND s.matchmaker_id = ?"
            params.append(self.p.user_id)
        return sql, params

    def dashboard(self) -> dict:
        scope, params = self._share_scope()
        by_mm = self.conn.execute(
            f"""SELECT u.name AS matchmaker, COUNT(*) AS shared,
                       SUM(s.outcome = 'accepted') AS accepted, SUM(s.outcome = 'rejected') AS rejected
                FROM shares s JOIN users u ON u.id = s.matchmaker_id
                WHERE {scope} AND s.outcome != 'pending' GROUP BY u.name ORDER BY u.name""",
            params,
        ).fetchall()
        reasons = self.conn.execute(
            f"""SELECT r.reason_code, r.reason_detail, COUNT(*) AS n FROM rejections r
                JOIN shares s ON s.id = r.share_id
                WHERE {scope} AND r.status = 'done' GROUP BY r.reason_code, r.reason_detail ORDER BY n DESC""",
            params,
        ).fetchall()
        return {
            "by_matchmaker": [dict(r) for r in by_mm],
            "reasons": [dict(r) for r in reasons],
        }

    def backtest(self) -> dict:
        """Replay last month's shares through the checker using today's preference cards."""
        scope, params = self._share_scope()
        rows = self.conn.execute(
            f"""SELECT s.client_id, s.outcome, u.name AS matchmaker, {', '.join('k.' + f.strip() for f in CANDIDATE_PUBLIC_FIELDS.split(','))}
                FROM shares s JOIN candidates k ON k.id = s.candidate_id JOIN users u ON u.id = s.matchmaker_id
                WHERE {scope} AND s.is_historical = 1""",
            params,
        ).fetchall()
        cards: dict[str, dict] = {}
        totals = {"shared": 0, "accepted": 0, "blocked": 0, "blocked_rejected": 0, "blocked_accepted": 0}
        per_mm: dict[str, dict] = {}
        rules: dict[str, int] = {}
        for r in rows:
            cand = dict(r)
            card = cards.setdefault(r["client_id"], self._card(r["client_id"]))
            violations = matching.hard_violations(card["hard"], cand)
            m = per_mm.setdefault(r["matchmaker"], {"shared": 0, "accepted": 0, "blocked": 0})
            totals["shared"] += 1
            m["shared"] += 1
            if r["outcome"] == "accepted":
                totals["accepted"] += 1
                m["accepted"] += 1
            if violations:
                totals["blocked"] += 1
                m["blocked"] += 1
                totals["blocked_rejected" if r["outcome"] == "rejected" else "blocked_accepted"] += 1
                for v in violations:
                    rules[v["label"]] = rules.get(v["label"], 0) + 1
        remaining = totals["shared"] - totals["blocked"]
        return {
            **totals,
            "acceptance_before": round(100 * totals["accepted"] / totals["shared"], 1) if totals["shared"] else 0,
            "acceptance_after": round(100 * totals["accepted"] / remaining, 1) if remaining else 0,
            "by_matchmaker": [
                {
                    "matchmaker": name,
                    **m,
                    "acceptance_before": round(100 * m["accepted"] / m["shared"], 1),
                    "acceptance_after": round(100 * m["accepted"] / (m["shared"] - m["blocked"]), 1)
                    if m["shared"] > m["blocked"] else 0,
                }
                for name, m in sorted(per_mm.items())
            ],
            "blocked_by_rule": dict(sorted(rules.items(), key=lambda kv: -kv[1])),
        }

    # ------------------------------------------------------------ audit

    def audit(self, action: str, entity: str, entity_id: str | None = None, detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (tenant_id, actor_id, action, entity, entity_id, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.p.tenant_id, self.p.user_id, action, entity, entity_id, detail, now_iso()),
        )

    def audit_log(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT a.at, u.name AS actor, a.action, a.entity, a.entity_id, a.detail
               FROM audit_log a JOIN users u ON u.id = a.actor_id
               WHERE a.tenant_id = ? ORDER BY a.id DESC LIMIT ?""",
            (self.p.tenant_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
