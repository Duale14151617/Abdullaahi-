import { useState } from "react";
import { api } from "../api/client";
import { useFetch } from "../api/useFetch";
import { useAuth } from "../auth/AuthContext";
import { Chip, ErrorNotice, Field, Notice, PageHeader, TabPanel, Tabs } from "../components/ui";
import { UnitPicker } from "../components/UnitPicker";
import { useI18n, type Key } from "../i18n";

interface Named { id: string; code: string; name_en: string; name_so: string }
type State = "eligible" | "not_yet" | "blocked" | "no_rule";
interface Row {
  personnel: { id: string; full_name: string; service_number: string | null; status: string };
  unit: Omit<Named, "id">; rank: Named; next_rank: Named | null; rank_since: string; since_source: "history" | "record";
  months: number; required: number | null; months_left: number | null; state: State;
}
interface RankRule { id: string; code: string; name_en: string; name_so: string; rule: { min_months: number; active: boolean; next_rank: Named | null } | null }
const TONE: Record<State, string> = { eligible: "ok", not_yet: "neutral", blocked: "bad", no_rule: "warn" };

/** Who has served long enough in their rank. Advisory: it never changes a rank. */
export function Promotion() {
  const { t } = useI18n();
  const [tab, setTab] = useState<"list" | "rules">("list");
  return (
    <>
      <PageHeader title={t("pm.title")} subtitle={t("pm.subtitle")} />
      <Tabs label={t("pm.title")} value={tab} onChange={setTab} controls idPrefix="pm"
        tabs={[{ id: "list", label: t("pm.tab.list") }, { id: "rules", label: t("pm.tab.rules") }]} />
      <TabPanel id="list" active={tab === "list"} idPrefix="pm"><List /></TabPanel>
      <TabPanel id="rules" active={tab === "rules"} idPrefix="pm"><Rules /></TabPanel>
    </>
  );
}

function List() {
  const { t, lang, name } = useI18n();
  const [unit, setUnit] = useState("");
  const [state, setState] = useState("");
  const data = useFetch<{ counts: Record<State, number>; results: Row[] }>(unit ? "/promotion/eligibility/" : null, { org_unit: unit, state: state || undefined });
  const day = (s: string) => new Date(`${s}T00:00:00`).toLocaleDateString(lang === "so" ? "so-SO" : "en-GB", { dateStyle: "medium" });
  return (
    <section className="card">
      <UnitPicker label={t("pm.unit")} value={unit} onChange={setUnit} />
      <ErrorNotice error={data.error} />
      {!unit && <p className="muted">{t("pm.pick")}</p>}
      {unit && data.data && (
        <>
          <div className="row">
            {(["eligible", "not_yet", "blocked", "no_rule"] as const).map((s) => (
              <Chip key={s} tone={TONE[s]}>{t(`pm.state.${s}` as Key)}: {data.data!.counts[s]}</Chip>))}
          </div>
          <Field label={t("pm.state")}>
            <select value={state} onChange={(e) => setState(e.target.value)}>
              <option value="">{t("common.all")}</option>
              {(["eligible", "not_yet", "blocked", "no_rule"] as const).map((s) => <option key={s} value={s}>{t(`pm.state.${s}` as Key)}</option>)}
            </select>
          </Field>
          {data.data.results.length === 0 && <p className="muted">{t("pm.noRows")}</p>}
          <ul className="list">
            {data.data.results.map((r) => (
              <li key={r.personnel.id} className="item">
                <div className="row">
                  <b>{r.personnel.full_name}</b>
                  <span>{name(r.rank)}{r.next_rank ? ` → ${name(r.next_rank)}` : ""}</span>
                  <Chip tone={TONE[r.state]}>{t(`pm.state.${r.state}` as Key)}</Chip>
                </div>
                <p className="muted">
                  {t("pm.since")} {day(r.rank_since)} · {t("pm.months", { n: r.months })}
                  {r.required !== null ? ` ${t("pm.of", { n: r.required })}` : ""}
                  {r.state === "not_yet" && r.months_left ? ` · ${t("pm.left", { n: r.months_left })}` : ""}
                </p>
                {r.since_source === "record" && <p className="muted">{t("pm.sinceRecord")}</p>}
              </li>))}
          </ul>
        </>)}
    </section>
  );
}

function Rules() {
  const { t, name } = useI18n();
  const { can } = useAuth();
  const canSet = can("promotion.configure");
  const ranks = useFetch<{ results: RankRule[] }>("/promotion/rules/");
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});

  async function save(r: RankRule) {
    setError(null); setSaved(false);
    const months = Number(draft[r.id] ?? r.rule?.min_months);
    try {
      await api("/promotion/rules/set/", { method: "POST", body: { rank: r.id, min_months: months, next_rank: r.rule?.next_rank?.id } });
      setDraft((d) => Object.fromEntries(Object.entries(d).filter(([k]) => k !== r.id))); setSaved(true); ranks.reload();
    } catch (err) { setError(err); }
  }
  async function remove(r: RankRule) {
    setError(null); setSaved(false);
    try { await api("/promotion/rules/set/", { method: "POST", body: { rank: r.id, remove: true } }); ranks.reload(); }
    catch (err) { setError(err); }
  }

  return (
    <section className="card">
      <p className="muted">{t("pm.rules.hint")}</p>
      <ErrorNotice error={error ?? ranks.error} />
      {saved && <Notice>{t("pm.rules.saved")}</Notice>}
      <ul className="list">
        {(ranks.data?.results ?? []).map((r) => (
          <li key={r.id} className="item">
            <div className="row">
              <b>{name(r)}</b>
              {!canSet && <span>{r.rule ? t("pm.months", { n: r.rule.min_months }) : t("pm.rules.none")}</span>}
              {canSet && (
                <>
                  <Field label={t("pm.rules.months")}>
                    <input type="number" min={1} max={600} value={draft[r.id] ?? String(r.rule?.min_months ?? "")}
                           onChange={(e) => setDraft((d) => ({ ...d, [r.id]: e.target.value }))} />
                  </Field>
                  <button type="button" disabled={!(draft[r.id] ?? r.rule)} onClick={() => void save(r)}>{t("pm.rules.save")}</button>
                  {r.rule && <button type="button" className="secondary" onClick={() => void remove(r)}>{t("pm.rules.remove")}</button>}
                </>)}
            </div>
          </li>))}
      </ul>
    </section>
  );
}
