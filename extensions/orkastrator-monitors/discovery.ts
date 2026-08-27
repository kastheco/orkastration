import { lstat, readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

import { type MonitorTask, parseMonitorTask } from "./core.ts";

const MAX_METADATA_BYTES = 256 * 1024;

export function processIsAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

async function readTaskFile(
  path: string,
  isProcessAlive: (pid: number) => boolean,
): Promise<MonitorTask | undefined> {
  try {
    const stats = await lstat(path);
    if (!stats.isFile() || stats.isSymbolicLink() || stats.size > MAX_METADATA_BYTES) return undefined;
    const parsed: unknown = JSON.parse(await readFile(path, "utf8"));
    return parseMonitorTask(parsed, path, isProcessAlive);
  } catch {
    return undefined;
  }
}

export async function discoverMonitorTasks(
  cwd: string,
  isProcessAlive: (pid: number) => boolean = processIsAlive,
): Promise<MonitorTask[]> {
  const tasksRoot = join(cwd, ".pi", "tasks");
  let sessionEntries;
  try {
    sessionEntries = await readdir(tasksRoot, { withFileTypes: true });
  } catch {
    return [];
  }

  const paths: string[] = [];
  for (const sessionEntry of sessionEntries) {
    if (!sessionEntry.isDirectory() || sessionEntry.isSymbolicLink() || !sessionEntry.name.startsWith("session-")) {
      continue;
    }
    const sessionPath = join(tasksRoot, sessionEntry.name);
    try {
      const taskEntries = await readdir(sessionPath, { withFileTypes: true });
      for (const taskEntry of taskEntries) {
        if (taskEntry.isFile() && !taskEntry.isSymbolicLink() && taskEntry.name.endsWith(".json")) {
          paths.push(join(sessionPath, taskEntry.name));
        }
      }
    } catch {
      // A concurrently removed or unreadable session directory is not fatal.
    }
  }

  const parsed = (await Promise.all(paths.sort().map((path) => readTaskFile(path, isProcessAlive)))).filter(
    (task): task is MonitorTask => task !== undefined,
  );
  const newestById = new Map<string, MonitorTask>();
  for (const task of parsed) {
    const previous = newestById.get(task.id);
    if (!previous || task.startTime > previous.startTime) newestById.set(task.id, task);
  }
  return [...newestById.values()];
}
