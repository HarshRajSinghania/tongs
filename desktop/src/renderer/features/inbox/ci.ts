import type {
  CIStatusValue,
  DesktopBridge,
  ReviewListItemDto,
} from "../../../shared/bridge.js";

/**
 * Most CI lookups one review list keeps in flight. Each lookup is one sidecar
 * request, and the main process refuses a read once 64 are pending, so the
 * list reads and these lookups together stay well below that. The service
 * also holds every forge request to its per-host limit.
 */
export const CI_LOOKUP_CONCURRENCY = 4;

/**
 * Fills in the CI status of listed reviews one request each.
 *
 * A list read leaves a review's CI `unknown` when the forge would need a
 * request per row to know it. Only rows handed to `request` are looked up, in
 * that order and a few at a time. `reset` starts a new generation: it cancels
 * the lookups in flight, drops the queue and every status found so far, so a
 * late result of an older list never lands on a newer one.
 */
export class CILookups {
  private generation = 0;
  private fresh = false;
  private active = 0;
  private readonly queue: string[] = [];
  private readonly queued = new Set<string>();
  private readonly found = new Map<string, CIStatusValue>();
  private readonly inFlight = new Set<string>();

  constructor(
    private readonly bridge: DesktopBridge,
    private readonly changed: () => void,
    private readonly concurrency = CI_LOOKUP_CONCURRENCY,
  ) {}

  /** The status a lookup found for this row, if it has finished. */
  status(handle: string): CIStatusValue | undefined {
    return this.found.get(handle);
  }

  /** Whether the row still waits for its lookup. */
  pending(item: ReviewListItemDto): boolean {
    return item.summary.ci_status === "unknown" && !this.found.has(item.handle);
  }

  /** The CI status to show for a row; null while its lookup runs. */
  shown(item: ReviewListItemDto): string | null {
    if (item.summary.ci_status !== "unknown") return item.summary.ci_status;
    return this.found.get(item.handle) ?? null;
  }

  /** How many lookups are queued or in flight. */
  get waiting(): number {
    return this.queued.size;
  }

  /** Queue lookups for the rows that still wait, keeping their order. */
  request(items: readonly ReviewListItemDto[]): void {
    for (const item of items) {
      if (!this.pending(item) || this.queued.has(item.handle)) continue;
      this.queued.add(item.handle);
      this.queue.push(item.handle);
    }
    this.pump();
  }

  /**
   * Start over for a new list. `fresh` makes this generation's lookups read
   * past the service's CI cache, as after an explicit refresh.
   */
  reset(fresh = false): void {
    const hadResults = this.found.size > 0;
    this.stop();
    this.fresh = fresh;
    if (hadResults) this.changed();
  }

  /** Cancel every lookup and forget the queue, as when the list goes away. */
  stop(): void {
    this.generation += 1;
    this.active = 0;
    this.queue.length = 0;
    this.queued.clear();
    this.found.clear();
    for (const token of this.inFlight)
      void this.bridge.cancelRead(token).catch(() => false);
    this.inFlight.clear();
  }

  private pump(): void {
    while (this.active < this.concurrency && this.queue.length > 0) {
      const handle = this.queue.shift() as string;
      this.active += 1;
      void this.lookup(handle, this.generation);
    }
  }

  private async lookup(handle: string, generation: number): Promise<void> {
    let token: string | null = null;
    let status: CIStatusValue = "unknown";
    try {
      const read = this.bridge.getReviewCIStatus(
        this.fresh ? { review: handle, fresh: true } : { review: handle },
      );
      token = read.requestToken;
      this.inFlight.add(token);
      status = (await read.result).ci_status;
    } catch {
      // A failed lookup leaves the status unknown instead of loading forever.
      status = "unknown";
    } finally {
      if (token !== null) this.inFlight.delete(token);
    }
    if (generation !== this.generation) return;
    this.active -= 1;
    this.queued.delete(handle);
    this.found.set(handle, status);
    this.changed();
    this.pump();
  }
}
