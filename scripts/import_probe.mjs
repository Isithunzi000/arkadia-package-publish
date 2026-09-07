// Sonda importowa bundle'a Dargoth.
// Importuje bundle ESM, wola init(api) na uniwersalnej atraphie PluginApi
// (Proxy) i wypisuje na stdout posortowany slad rejestracji jako JSON:
// { pluginInfo, aliases, triggers, popups, menus }
// Uzycie: node scripts/import_probe.mjs <sciezka-do-bundle.js>

import { pathToFileURL } from "node:url";

// --- Atrapki DOM (init pluginu moze dotykac document/window/localStorage) ---

function makeFakeEl() {
  const target = function () {};
  return new Proxy(target, {
    get(_t, prop) {
      if (prop === "then") return undefined; // await na atrapie nie moze wisiec
      if (typeof prop === "symbol") return undefined;
      return fakeEl;
    },
    set() { return true; },
    apply() { return fakeEl; },
    has() { return true; },
  });
}
const fakeEl = makeFakeEl();

globalThis.window = globalThis.window ?? fakeEl;
globalThis.document = globalThis.document ?? fakeEl;
globalThis.localStorage = globalThis.localStorage ?? {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};
globalThis.navigator = globalThis.navigator ?? { userAgent: "verify-probe" };

// --- Uniwersalna atrapia PluginApi z rejestron wywolan ---

const calls = [];

function serializeArg(arg) {
  if (arg instanceof RegExp) return `${arg.source}|${arg.flags}`;
  if (typeof arg === "function") return "[function]";
  if (arg === undefined) return "[undefined]";
  try {
    return JSON.parse(JSON.stringify(arg));
  } catch {
    return "[unserializable]";
  }
}

function makeApi(path) {
  const target = function () {};
  return new Proxy(target, {
    get(_t, prop) {
      if (prop === "then") return undefined;
      if (typeof prop === "symbol") return undefined;
      return makeApi([...path, String(prop)]);
    },
    set() { return true; },
    apply(_t, _this, args) {
      calls.push({ path: path.join("."), args: args.map(serializeArg) });
      return undefined; // await undefined jest bezpieczny
    },
  });
}

// --- Import i sonda ---

const bundlePath = process.argv[2];
if (!bundlePath) {
  console.error("uzycie: node import_probe.mjs <bundle.js>");
  process.exit(2);
}

const mod = await import(pathToFileURL(bundlePath).href);
if (typeof mod.init !== "function") {
  console.error("bundle nie eksportuje init()");
  process.exit(3);
}

const api = makeApi([]);
const pluginInfo = (await mod.init(api)) ?? {};

const str = (v) => (typeof v === "string" ? v : JSON.stringify(v));
const uniq = (arr) => [...new Set(arr)].sort();

const footprint = {
  pluginInfo,
  aliases: uniq(calls.filter((c) => c.path === "aliases.register").map((c) => str(c.args[0]))),
  triggers: uniq(calls.filter((c) => c.path === "triggers.register").map((c) => str(c.args[0]))),
  popups: uniq(
    calls
      .filter((c) => c.path === "ui.registerPersistentPopup")
      .map((c) => {
        const a = c.args[0];
        return a && typeof a === "object" ? `${a.id}|${a.title}` : str(a);
      }),
  ),
  menus: uniq(calls.filter((c) => c.path === "ui.addPopupMenuEntry").map((c) => str(c.args[0]))),
};

process.stdout.write(JSON.stringify(footprint) + "\n");
