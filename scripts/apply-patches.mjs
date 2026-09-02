import { readFileSync } from "node:fs";
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

const workflowsRoot = dependencyRoot("@osolmaz/pi-workflows", "@osolmaz/pi-workflows");
const installRoot = dirname(dirname(dirname(workflowsRoot)));
const patchPackageRoot = dependencyRoot("patch-package", "patch-package");
const patchPackageManifest = JSON.parse(
  readFileSync(join(patchPackageRoot, "package.json"), "utf8"),
);
const bin = typeof patchPackageManifest.bin === "string"
  ? patchPackageManifest.bin
  : patchPackageManifest.bin["patch-package"];
if (typeof bin !== "string") throw new Error("patch-package executable is unavailable");

const patchDirectory = relative(installRoot, patches);
const result = spawnSync(
  process.execPath,
  [join(patchPackageRoot, bin), "--patch-dir", patchDirectory, "--error-on-fail"],
  { cwd: installRoot, stdio: "inherit" },
);
if (result.error !== undefined) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);
