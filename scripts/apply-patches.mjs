import { copyFileSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const packageRoot = dirname(fileURLToPath(new URL("../package.json", import.meta.url)));
const patches = join(packageRoot, "patches");

function dependencyRoot(entry, packageName) {
  let current = dirname(require.resolve(entry));
  while (true) {
    try {
      const manifest = JSON.parse(readFileSync(join(current, "package.json"), "utf8"));
      if (manifest.name === packageName) return current;
    } catch {
      // Keep walking until the package manifest is found.
    }
    const parent = dirname(current);
    if (parent === current) throw new Error(`Could not locate ${packageName}`);
    current = parent;
  }
}

const patchPackageRoot = dependencyRoot("patch-package", "patch-package");
const patchPackageManifest = JSON.parse(
  readFileSync(join(patchPackageRoot, "package.json"), "utf8"),
);
const bin = typeof patchPackageManifest.bin === "string"
  ? patchPackageManifest.bin
  : patchPackageManifest.bin["patch-package"];
if (typeof bin !== "string") throw new Error("patch-package executable is unavailable");

function applyDependencyPatch(entry, packageName, patchName) {
  const root = dependencyRoot(entry, packageName);
  const installRoot = dirname(dirname(dirname(root)));
  const temporaryPatches = mkdtempSync(join(installRoot, ".orkastrator-patches-"));
  try {
    copyFileSync(join(patches, patchName), join(temporaryPatches, patchName));
    const patchDirectory = relative(installRoot, temporaryPatches);
    const result = spawnSync(
      process.execPath,
      [join(patchPackageRoot, bin), "--patch-dir", patchDirectory, "--error-on-fail"],
      { cwd: installRoot, stdio: "inherit" },
    );
    if (result.error !== undefined) throw result.error;
    if (result.status !== 0) process.exit(result.status ?? 1);
  } finally {
    rmSync(temporaryPatches, { recursive: true, force: true });
  }
}

applyDependencyPatch(
  "@osolmaz/pi-workflows",
  "@osolmaz/pi-workflows",
  "@osolmaz+pi-workflows+0.16.0.patch",
);
applyDependencyPatch(
  "@juicesharp/rpiv-ask-user-question",
  "@juicesharp/rpiv-ask-user-question",
  "@juicesharp+rpiv-ask-user-question+2.9.0.patch",
);
