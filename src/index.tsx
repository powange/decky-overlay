import {
  ButtonItem,
  Dropdown,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, fetchNoCors } from "@decky/api";
import { useEffect, useState } from "react";
import { FaLayerGroup } from "react-icons/fa";

// --------------------------------------------------------------------------
// Backend
// --------------------------------------------------------------------------

interface Settings {
  enabled: boolean;
  size: string;
  opacity: number;
  display: string;
  hidden_clients: string[];
}

interface ClientInfo {
  id: string;
  name: string;
  blocks: number;
  hidden: boolean;
}

interface State {
  settings: Settings;
  clients: ClientInfo[];
  renderer_running: boolean;
  renderer_error: string;
  socket_error: string;
  socket_path: string;
  game_mode: boolean;
  log_dir: string;
}

const getState = callable<[], State>("get_state");
const getVersion = callable<[], string>("get_version");
const saveSettings = callable<[settings: Partial<Settings>], State>("save_settings");
const preview = callable<[], State>("preview");

let pendingSettings: Partial<Settings> = {};
let settingsTimer: ReturnType<typeof setTimeout> | null = null;
// Sliders send many values: save once the user stops moving them.
function queueSettingsSave(patch: Partial<Settings>, onSaved: (s: State) => void) {
  pendingSettings = { ...pendingSettings, ...patch };
  if (settingsTimer) clearTimeout(settingsTimer);
  settingsTimer = setTimeout(async () => {
    const toSave = pendingSettings;
    pendingSettings = {};
    settingsTimer = null;
    try {
      onSaved(await saveSettings(toSave));
    } catch (e) {
      console.error("Decky Overlay: settings not saved", e);
    }
  }, 300);
}

// --------------------------------------------------------------------------
// Updates (same mechanism as powange/decky-macro)
// --------------------------------------------------------------------------

// Releases are published by .github/workflows/release.yml.
const UPDATE_REPO = "powange/decky-overlay";
const UPDATE_ASSET = "decky-overlay.zip";
const MANUAL_INSTALL_URL = `https://github.com/${UPDATE_REPO}/releases/latest/download/${UPDATE_ASSET}`;
// Must match "name" in plugin.json so Decky replaces the installed plugin.
const PLUGIN_NAME = "Decky Overlay";
// Decky's PluginInstallType.UPDATE
const INSTALL_TYPE_UPDATE = 2;

let updateCheckedThisSession = false;

// Compares "v1.2.3" / "1.2.3" numerically: >0 when a is newer than b.
function compareVersions(a: string, b: string): number {
  const parse = (v: string) => v.replace(/^v/, "").split(/[.-]/).map((x) => parseInt(x, 10) || 0);
  const pa = parse(a);
  const pb = parse(b);
  for (let i = 0; i < Math.max(pa.length, pb.length, 3); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

function UpdateSection() {
  const [current, setCurrent] = useState<string>("");
  const [latest, setLatest] = useState<{ tag: string; url: string } | null>(null);
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  const [manualInstall, setManualInstall] = useState<boolean>(false);

  const check = async (version: string, silent = false) => {
    setBusy(true);
    if (!silent) setStatus("Vérification sur GitHub…");
    setLatest(null);
    try {
      const res = await fetchNoCors(`https://api.github.com/repos/${UPDATE_REPO}/releases/latest`, {
        headers: { Accept: "application/vnd.github+json" },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const rel = await res.json();
      const asset = (rel.assets || []).find((a: any) => a.name === UPDATE_ASSET);
      if (!asset) throw new Error(`pas de ${UPDATE_ASSET} dans la dernière release`);
      if (compareVersions(rel.tag_name, version) <= 0) {
        setStatus(`À jour (${rel.tag_name})`);
      } else {
        setLatest({ tag: rel.tag_name, url: asset.browser_download_url });
        setStatus(`Mise à jour disponible : ${rel.tag_name}`);
      }
    } catch (e: any) {
      if (!silent) setStatus(`Échec de la vérification : ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    getVersion()
      .then((v) => {
        setCurrent(v);
        // One silent check per plugin load, so an update shows up by itself.
        if (!updateCheckedThisSession) {
          updateCheckedThisSession = true;
          check(v, true);
        }
      })
      .catch(() => setCurrent("?"));
  }, []);

  const install = async () => {
    if (!latest) return;
    // Internal Decky API (undocumented): if a Decky update breaks it, fall
    // back to the manual install URL instead of failing silently.
    const backend = (window as any).DeckyBackend;
    if (typeof backend?.call !== "function") {
      setManualInstall(true);
      setStatus("Mise à jour automatique indisponible avec cette version de Decky.");
      return;
    }
    try {
      setBusy(true);
      // Opens Decky's own install confirmation, like the store does.
      await backend.call(
        "utilities/install_plugin",
        latest.url,
        PLUGIN_NAME,
        latest.tag.replace(/^v/, ""),
        "",
        INSTALL_TYPE_UPDATE
      );
    } catch (e: any) {
      setManualInstall(true);
      setStatus(`Échec de l'installation : ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <PanelSection title="Mises à jour">
      <PanelSectionRow>
        <div style={{ fontSize: 12, color: "#bbb" }}>
          Installée : <b>{current || "…"}</b>
          {status && <div>{status}</div>}
          {manualInstall && (
            <div style={{ marginTop: 4, wordBreak: "break-all" }}>
              Installe-la depuis Decky › Développeur › Installer depuis une URL :
              <div style={{ fontFamily: "monospace", color: "#ddd" }}>{MANUAL_INSTALL_URL}</div>
            </div>
          )}
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        {latest ? (
          <ButtonItem layout="below" onClick={install}>
            Mettre à jour vers {latest.tag}
          </ButtonItem>
        ) : (
          <ButtonItem layout="below" onClick={() => check(current)} disabled={busy}>
            Rechercher une mise à jour
          </ButtonItem>
        )}
      </PanelSectionRow>
    </PanelSection>
  );
}


// --------------------------------------------------------------------------
// Main panel
// --------------------------------------------------------------------------

const sizeOptions = [
  { data: "small", label: "Petite" },
  { data: "medium", label: "Moyenne" },
  { data: "large", label: "Grande" },
  { data: "xlarge", label: "Très grande" },
];

function Content() {
  const [state, setState] = useState<State | null>(null);

  useEffect(() => {
    const refresh = () => getState().then(setState).catch(() => {});
    refresh();
    // Clients connect and disconnect on their own: keep the list fresh.
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, []);

  if (!state) {
    return (
      <PanelSection>
        <PanelSectionRow>Chargement…</PanelSectionRow>
      </PanelSection>
    );
  }

  const { settings } = state;
  const update = (patch: Partial<Settings>) => {
    setState((prev) => (prev ? { ...prev, settings: { ...prev.settings, ...patch } } : prev));
    queueSettingsSave(patch, setState);
  };

  const status = state.socket_error
    ? { text: state.socket_error, color: "#ff8080" }
    : state.renderer_error
      ? { text: state.renderer_error, color: "#ff8080" }
      : !state.game_mode
        ? { text: "Hors du mode jeu : rien n'est affiché sur le bureau.", color: "#999" }
        : state.renderer_running
          ? { text: "Overlay affiché.", color: "#7c7" }
          : { text: "Prêt : rien à afficher pour l'instant.", color: "#999" };

  return (
    <>
      <PanelSection title="Overlay">
        <PanelSectionRow>
          <div style={{ fontSize: 12, color: status.color }}>
            {status.text}
            {(state.renderer_error || state.socket_error) && (
              <div style={{ color: "#999" }}>Détails : {state.log_dir}/renderer.log</div>
            )}
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField label="Afficher l'overlay" checked={settings.enabled} onChange={(v) => update({ enabled: v })} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Taille" childrenContainerWidth="max">
            <Dropdown rgOptions={sizeOptions} selectedOption={settings.size} onChange={(o) => update({ size: o.data })} />
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Opacité du fond"
            value={Math.round(settings.opacity * 100)}
            min={20}
            max={100}
            step={5}
            showValue
            onChange={(v) => update({ opacity: v / 100 })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={async () => setState(await preview())}>
            Aperçu (10 s)
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Plugins connectés">
        {state.clients.length === 0 ? (
          <PanelSectionRow>
            <div style={{ fontSize: 12, color: "#999" }}>
              Aucun plugin connecté. Les plugins compatibles (comme Decky Timer) apparaissent ici.
            </div>
          </PanelSectionRow>
        ) : (
          state.clients.map((c) => (
            <PanelSectionRow key={c.id}>
              <ToggleField
                label={c.name}
                description={`${c.blocks} élément${c.blocks > 1 ? "s" : ""} à afficher`}
                checked={!c.hidden}
                onChange={(visible) => {
                  const hidden = new Set(settings.hidden_clients);
                  if (visible) hidden.delete(c.id);
                  else hidden.add(c.id);
                  update({ hidden_clients: [...hidden] });
                }}
              />
            </PanelSectionRow>
          ))
        )}
      </PanelSection>

      <UpdateSection />
    </>
  );
}

export default definePlugin(() => ({
  name: "Decky Overlay",
  titleView: <div className={staticClasses.Title}>Decky Overlay</div>,
  content: <Content />,
  icon: <FaLayerGroup />,
}));
