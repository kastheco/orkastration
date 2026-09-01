export function currentHerdrPaneId(env: NodeJS.ProcessEnv = process.env): string {
  const paneId = env.HERDR_PANE_ID;
  if (typeof paneId !== "string" || paneId.trim().length === 0) {
    throw new Error("Orkastrator cannot place workers because HERDR_PANE_ID is not set");
  }
  return paneId;
}
