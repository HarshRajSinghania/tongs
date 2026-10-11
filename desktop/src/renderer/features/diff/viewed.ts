import { useCallback, useSyncExternalStore } from "react";
import type { DiffFileRow } from "../../../shared/bridge.js";

/**
 * Which files of a review the reader has marked as viewed (#195).
 *
 * The marks live in this module for the life of the renderer, so they survive
 * a tab switch, a layout switch and a Refresh that brings back the same
 * revision, and are gone when the app restarts. Each review's marks belong to
 * the head commit they were made on: a diff read at a different head SHA
 * starts with nothing viewed, because new commits can change any file.
 */

/** The most reviews a session remembers marks for; the oldest is dropped. */
export const MAX_VIEWED_REVIEWS = 200;

interface ViewedRecord {
  readonly headSha: string;
  readonly files: ReadonlySet<string>;
}

const records = new Map<string, ViewedRecord>();
const listeners = new Set<() => void>();
const NOTHING_VIEWED: ReadonlySet<string> = new Set<string>();

/**
 * A file's identity across reads of the same revision. The file index is the
 * position in one read, so the paths name the file instead.
 */
export function viewedFileKey(
  file: Pick<DiffFileRow, "old_path" | "new_path">,
): string {
  return `${file.old_path}\u0000${file.new_path}`;
}

/** The files marked as viewed for `review` at `headSha`. */
export function viewedFilesFor(
  review: string,
  headSha: string,
): ReadonlySet<string> {
  const record = records.get(review);
  return record !== undefined && record.headSha === headSha
    ? record.files
    : NOTHING_VIEWED;
}

/**
 * Marks or clears one file. A mark made at a new head SHA replaces whatever
 * the review held for an older one.
 */
export function setFileViewed(
  review: string,
  headSha: string,
  key: string,
  viewed: boolean,
): void {
  const current = viewedFilesFor(review, headSha);
  if (current.has(key) === viewed) return;
  const files = new Set(current);
  if (viewed) files.add(key);
  else files.delete(key);
  // Reinserting keeps the map in least recently marked order.
  records.delete(review);
  records.set(review, Object.freeze({ headSha, files }));
  while (records.size > MAX_VIEWED_REVIEWS) {
    const oldest = records.keys().next();
    if (oldest.done === true) break;
    records.delete(oldest.value);
  }
  notify();
}

/** Forgets every mark. The app never calls it; tests start clean with it. */
export function clearViewedFiles(): void {
  if (records.size === 0) return;
  records.clear();
  notify();
}

function notify(): void {
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export interface ViewedFiles {
  readonly files: ReadonlySet<string>;
  readonly setViewed: (key: string, viewed: boolean) => void;
}

/** The viewed marks for one review at one head SHA, kept current. */
export function useViewedFiles(review: string, headSha: string): ViewedFiles {
  const files = useSyncExternalStore(subscribe, () =>
    viewedFilesFor(review, headSha),
  );
  const setViewed = useCallback(
    (key: string, viewed: boolean) =>
      setFileViewed(review, headSha, key, viewed),
    [review, headSha],
  );
  return { files, setViewed };
}

/** "N of M files viewed", counting only the files the diff holds. */
export function viewedProgressText(
  files: readonly DiffFileRow[],
  viewed: ReadonlySet<string>,
): string {
  let count = 0;
  for (const file of files) if (viewed.has(viewedFileKey(file))) count += 1;
  return `${count} of ${files.length} ${files.length === 1 ? "file" : "files"} viewed`;
}
