import { request } from './client'

/** 快照发布对账接口：发布、凭据回读、账本还原、字段追踪、跨组对比与差异再生成。 */

export type FieldChange = {
  field: string
  before: string
  after: string
  schema_before?: string
  schema_after?: string
}

export type PublishResult = {
  ok: boolean
  message: string
  deduplicated?: boolean
  receipt_token?: string
  release?: Record<string, unknown>
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return (await response.json()) as T
}

export function publishSnapshotRelease(
  assetCode: string,
  baselineTag: string,
  group: string,
  fieldChanges: FieldChange[],
  receiptToken?: string,
): Promise<PublishResult> {
  return postJson('/api/snapshot-releases/publish', {
    asset_code: assetCode,
    baseline_tag: baselineTag,
    group,
    field_changes: fieldChanges,
    receipt_token: receiptToken,
  })
}

export function replayReceipt(receiptToken: string): Promise<PublishResult> {
  return postJson('/api/snapshot-releases/replay', { receipt_token: receiptToken })
}

export function readRelease(
  releaseId: string,
  receiptToken: string,
): Promise<{ ok: boolean; message?: string; release?: Record<string, unknown> }> {
  return postJson('/api/snapshot-releases/read', {
    release_id: releaseId,
    receipt_token: receiptToken,
  })
}

export function restoreLedger(
  receiptToken: string,
): Promise<{ ok: boolean; message?: string; ledger?: Record<string, unknown> }> {
  return postJson('/api/snapshot-releases/restore-ledger', {
    receipt_token: receiptToken,
  })
}

export function traceField(
  receiptToken: string,
  fieldName: string,
  expectedValue?: string,
): Promise<{ ok: boolean; message?: string; trace?: Record<string, unknown> }> {
  return postJson('/api/snapshot-releases/trace-field', {
    receipt_token: receiptToken,
    field_name: fieldName,
    expected_value: expectedValue,
  })
}

export function compareReleases(
  receiptA: string,
  receiptB: string,
): Promise<{ ok: boolean; message?: string; comparison?: Record<string, unknown> }> {
  return postJson('/api/snapshot-releases/compare', {
    receipt_a: receiptA,
    receipt_b: receiptB,
  })
}

export function advanceHead(
  releaseId: string,
  receiptToken: string,
): Promise<{ ok: boolean; message?: string; 主版本?: string }> {
  return postJson('/api/snapshot-releases/advance-head', {
    release_id: releaseId,
    receipt_token: receiptToken,
  })
}

export function regenerateReleaseDiff(
  releaseId: string,
  receiptToken: string,
): Promise<{ ok: boolean; message?: string; diff?: Record<string, unknown> }> {
  return postJson('/api/snapshot-releases/regenerate-diff', {
    release_id: releaseId,
    receipt_token: receiptToken,
  })
}

export async function readReleaseChanges(
  releaseId: string,
): Promise<{ ok: boolean; message?: string; summary?: Record<string, unknown> }> {
  const response = await request(`/api/snapshot-releases/changes/${releaseId}`)
  return await response.json()
}
