"""Platform entry metadata; AI/knowledge stay owned by the bound instance.

No Chatwoot inbox, bot, conversation, credential or profile is deleted here.
Reads do not create storage. First mutation incrementally migrates schema 4.
"""
import json
import re

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .config import RuntimeFailure


SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")


def slug_value(value):
    if not isinstance(value, str) or len(value) > 64 or not SLUG.fullmatch(value):
        raise RuntimeFailure("invalid_support_slug", 422)
    return value


class EntryFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=128)
    service_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    account_id: int = Field(gt=0, le=2_147_483_647)
    inbox_id: int = Field(gt=0, le=2_147_483_647)
    enabled: bool = True


class EntryCreate(EntryFields):
    slug: str = Field(min_length=1, max_length=64)


class EntryUpdate(EntryFields):
    slug: str | None = Field(default=None, min_length=1, max_length=64)


class SupportEntries:
    def __init__(self, bridge):
        self.bridge, self.store = bridge, bridge.store

    @staticmethod
    def _rows(db):
        if db is None or db.execute("PRAGMA user_version").fetchone()[0] < 5:
            return []
        return [{"slug": r[0], "name": r[1], "service_id": r[2], "account_id": r[3],
                 "inbox_id": r[4], "enabled": bool(r[5])} for r in db.execute(
                     "SELECT slug,name,service_id,account_id,inbox_id,enabled FROM support_entries ORDER BY slug")]

    @staticmethod
    def default(db):
        if db is None or db.execute("PRAGMA user_version").fetchone()[0] < 2:
            return {}
        row = db.execute("SELECT config FROM runtime_profiles WHERE kind='support-entry' AND key='default'").fetchone()
        return json.loads(row[0]) if row else {}

    def read(self):
        with self.store._connection() as db:
            return self._rows(db)

    def get(self, slug):
        slug_value(slug)
        row = next((r for r in self.read() if r["slug"] == slug), None)
        if row is None:
            raise RuntimeFailure("support_entry_not_found", 404)
        return row

    def binding(self, entry):
        services = self.store.read_services()
        sid = entry["service_id"]
        if sid not in services:
            raise RuntimeFailure("invalid_support_inbox", 409)
        binding = self.bridge.binding(sid)
        if not binding or (binding["account_id"], binding["inbox_id"]) != (entry["account_id"], entry["inbox_id"]):
            raise RuntimeFailure("invalid_support_inbox", 409)
        # Website pages/API/assets use OUR Rails. An external connection's
        # public website token must never be sent to the local Rails instance.
        local = self.bridge.local_origins()
        if local and not self.bridge.is_local_origin(binding["base_url"]):
            raise RuntimeFailure("support_requires_local_chatwoot", 409)
        self.bridge.require_unique_inbox(binding, binding["inbox_id"])
        return binding

    async def options(self):
        options = []
        for sid, service in self.store.read_services().items():
            binding = self.bridge.binding(sid)
            if not binding or not binding["inbox_id"]:
                continue
            try:
                self.binding({"service_id": sid, **{k: binding[k] for k in ("account_id", "inbox_id")}})
                inboxes = await self.bridge.inboxes(sid)
                inbox = next((r for r in inboxes if r["id"] == binding["inbox_id"]), None)
                if inbox:
                    options.append({"service_id": sid, "name": service["name"], "account_id": binding["account_id"],
                        "inbox_id": inbox["id"], "inbox_name": inbox["name"], "service_enabled": service["enabled"],
                        "ai_enabled": bool(binding["enabled"]),
                        **{k: service[k] for k in ("ai_config", "prompt_config", "knowledge_config")}})
            except RuntimeFailure:
                # A broken unrelated connection should not break working entries.
                continue
        return options

    async def save(self, slug, record, *, create=False):
        slug_value(slug)
        record = dict(record)
        target = slug_value(record.pop("slug", None) or slug)
        if not record["name"].strip() or any(ord(c) < 32 for c in record["name"]):
            raise RuntimeFailure("invalid_support_entry", 422)
        self.binding(record)
        if record["enabled"]:
            inboxes = await self.bridge.inboxes(record["service_id"])
            if record["inbox_id"] not in {r["id"] for r in inboxes}:
                raise RuntimeFailure("invalid_support_inbox", 422)
        async with self.bridge.setup_lock:
            # Recheck durable binding after network I/O, before saving metadata.
            self.binding(record)
            with self.store._connection(write=True) as db:
                rows = self._rows(db)
                exists = any(r["slug"] == slug for r in rows)
                if create and exists:
                    raise RuntimeFailure("support_slug_in_use", 409)
                if not create and not exists:
                    raise RuntimeFailure("support_entry_not_found", 404)
                if target != slug and any(r["slug"] == target for r in rows):
                    raise RuntimeFailure("support_slug_in_use", 409)
                if any(r["slug"] != slug and r["service_id"] == record["service_id"] for r in rows):
                    raise RuntimeFailure("support_inbox_in_use", 409)
                default = self.default(db)
                if default.get("slug") == slug and not record["enabled"]:
                    raise RuntimeFailure("support_default_protected", 409)
                if target != slug:
                    db.execute("UPDATE support_entries SET slug=? WHERE slug=?", (target, slug))
                db.execute("""INSERT INTO support_entries VALUES (?,?,?,?,?,?) ON CONFLICT(slug)
                    DO UPDATE SET name=excluded.name,service_id=excluded.service_id,account_id=excluded.account_id,
                    inbox_id=excluded.inbox_id,enabled=excluded.enabled""", (target, record["name"], record["service_id"],
                    record["account_id"], record["inbox_id"], int(record["enabled"])))
                if default.get("slug") == slug:
                    db.execute("UPDATE runtime_profiles SET config=? WHERE kind='support-entry' AND key='default'",
                               (json.dumps({"slug": target, "service_id": record["service_id"]}),))
        return self.get(target)

    def delete(self, slug):
        slug_value(slug)
        with self.store._connection(write=True) as db:
            if not any(r["slug"] == slug for r in self._rows(db)):
                raise RuntimeFailure("support_entry_not_found", 404)
            if self.default(db).get("slug") == slug:
                raise RuntimeFailure("support_default_protected", 409)
            db.execute("DELETE FROM support_entries WHERE slug=?", (slug,))
        return {"status": "deleted"}

    async def select(self, slug):
        entry = self.get(slug)
        self.binding(entry)
        if not entry["enabled"] or not self.store.read_services()[entry["service_id"]]["enabled"]:
            raise RuntimeFailure("support_entry_disabled", 409)
        from .support_api import SupportEntry
        await SupportEntry(self.bridge).public(slug)
        with self.store._connection(write=True) as db:
            # Serialize selection against deletion/disabling.
            current = next((r for r in self._rows(db) if r["slug"] == slug), None)
            if current != entry:
                raise RuntimeFailure("support_entry_changed", 409)
            db.execute("INSERT INTO runtime_profiles VALUES ('support-entry','default',?) ON CONFLICT(kind,key) DO UPDATE SET config=excluded.config",
                       (json.dumps({"slug": slug, "service_id": entry["service_id"]}),))
        return {"status": "saved"}


def entries_router(bridge):
    router = APIRouter(prefix="/support-entries")
    store = SupportEntries(bridge)

    @router.get("")
    def listing():
        with store.store._connection() as db:
            default = store.default(db).get("slug", "")
        return [{**r, "default": r["slug"] == default} for r in store.read()]

    @router.get("/options")
    async def options():
        return await store.options()

    @router.post("", status_code=201)
    async def create(body: EntryCreate):
        return await store.save(body.slug, body.model_dump(exclude={"slug"}), create=True)

    @router.put("/{slug}")
    async def update(slug: str, body: EntryUpdate):
        return await store.save(slug, body.model_dump())

    @router.delete("/{slug}")
    def delete(slug: str):
        return store.delete(slug)

    @router.post("/{slug}/default")
    async def select(slug: str):
        return await store.select(slug)

    return router
