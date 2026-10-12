import { useLive } from './liveContext.ts'

/** tenant -> its barcode servers, from gateway /admin/tenants (polled by LiveProvider); ag23 layout until then. */
export function useBarcodes(): Record<string, string[]> {
  return useLive().barcodes
}
