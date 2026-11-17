import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useFetch } from "../api/useFetch";
import { Chip, ErrorNotice, Field, PageHeader } from "../components/ui";
import { DataTable, type Column } from "../components/DataTable";
import { UnitPicker } from "../components/UnitPicker";
import { useI18n } from "../i18n";

interface UnitB { id: string; code: string; name_en: string; name_so: string }
interface Facility {
  id: string; code: string; kind: "jail_center" | "prison"; name_en: string; name_so: string; org_unit: UnitB | null; location: string;
  capacity: number | null; operated_by: string; active: boolean; version: number; held_now: number | null; received_total: number | null;
}
const clean = (f: Record<string, FormDataEntryValue>) => Object.fromEntries(Object.entries(f).filter(([, v]) => v !== ""));

export function CustodyFacilities() {
  const { t, name } = useI18n();
  const { can } = useAuth();
  const canEdit = can("detention.manage");
  const [kind, setKind] = useState("");
  const [adding, setAdding] = useState(false);
  const [newKind, setNewKind] = useState<"jail_center" | "prison">("jail_center");
  const [unit, setUnit] = useState("");
  const [error, setError] = useState<unknown>(null);
  const list = useFetch<{ results: Facility[] }>("/custody-facilities/", { kind: kind || undefined });

  async function post(path: string, body: Record<string, unknown>): Promise<boolean> {
    setError(null);
    try { await api(path, { method: "POST", body }); list.reload(); return true; } catch (e) { setError(e); return false; }
  }
  async function add(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const body = { ...clean(Object.fromEntries(new FormData(form).entries())), kind: newKind, ...(newKind === "jail_center" ? { org_unit: unit } : {}) };
    if (await post("/custody-facilities/", body)) { setAdding(false); form.reset(); }
  }

  const cols: Column<Facility>[] = [
    { id: "name", header: t("fac.nameEn"), cell: (x) => name(x), sort: (x) => x.name_en },
    { id: "code", header: t("fac.code"), cell: (x) => x.code, sort: (x) => x.code },
    { id: "kind", header: t("fac.kind"), cell: (x) => <Chip tone={x.kind === "prison" ? "draft" : "approved"}>{t(`fac.kind.${x.kind}` as never)}</Chip> },
    { id: "unit", header: t("fac.station"), cell: (x) => (x.org_unit ? name(x.org_unit) : "—") },
    { id: "by", header: t("fac.operator"), cell: (x) => x.operated_by || "—" },
    { id: "cap", header: t("fac.capacity"), align: "right", cell: (x) => (x.capacity ?? "—") },
    { id: "now", header: `${t("fac.heldNow")} / ${t("fac.received")}`, align: "right", cell: (x) => (x.kind === "jail_center" ? x.held_now ?? 0 : x.received_total ?? 0) },
    { id: "state", header: "", cell: (x) => (
      <>{x.active ? null : <Chip tone="bad">{t("fac.off")}</Chip>}
        {canEdit && <> <button type="button" className="secondary" onClick={() => void post(`/custody-facilities/${x.id}/edit/`, { version: x.version, active: !x.active })}>{x.active ? t("fac.switchOff") : t("fac.switchOn")}</button></>}</>) },
  ];

  return (
    <>
      <PageHeader title={t("fac.title")} subtitle={t("fac.subtitle")}
        actions={canEdit ? <button type="button" onClick={() => setAdding(!adding)}>{t("fac.add")}</button> : undefined} />
      <section className="card">
        <h3>{t("fac.explainJailTitle")}</h3>
        <p>{t("fac.explainJail")}</p>
        <h3>{t("fac.explainPrisonTitle")}</h3>
        <p>{t("fac.explainPrison")}</p>
      </section>
      <div className="row">
        <Field label={t("fac.kind")}>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">{t("fac.all")}</option>
            <option value="jail_center">{t("fac.kind.jail_center")}</option>
            <option value="prison">{t("fac.kind.prison")}</option>
          </select>
        </Field>
      </div>
      {error != null && <ErrorNotice error={error} />}
      {adding && (
        <form onSubmit={(e) => void add(e)} className="card">
          <Field label={t("fac.kind")}>
            <select value={newKind} onChange={(e) => setNewKind(e.target.value as "jail_center" | "prison")}>
              <option value="jail_center">{t("fac.kind.jail_center")}</option><option value="prison">{t("fac.kind.prison")}</option>
            </select>
          </Field>
          {newKind === "jail_center" && <UnitPicker label={t("fac.station")} value={unit} onChange={setUnit} required />}
          <Field label={t("fac.code")}><input name="code" required maxLength={64} /></Field>
          <Field label={t("fac.nameEn")}><input name="name_en" required maxLength={150} /></Field>
          <Field label={t("fac.nameSo")}><input name="name_so" maxLength={150} /></Field>
          <Field label={t("fac.location")}><input name="location" maxLength={200} /></Field>
          <Field label={t("fac.capacity")}><input name="capacity" type="number" min={0} inputMode="numeric" /></Field>
          <Field label={t("fac.operator")}><input name="operated_by" maxLength={150} /></Field>
          <button type="submit">{t("fac.save")}</button>
        </form>
      )}
      <DataTable id="custody-facilities" caption={t("fac.title")} columns={cols} rows={list.data?.results ?? []} rowKey={(x) => x.id} loading={list.loading} empty={t("fac.empty")} />
    </>
  );
}
