import type { KbItem } from '@/api/kb'

/** M8:重名知识库在下拉/tag 中追加 id 后缀,唯一者保持纯名(仅碰撞时视觉变化)。 */
export function disambiguateKbNames(
  kbs: Pick<KbItem, 'id' | 'name'>[],
): Map<number, string> {
  const counts = new Map<string, number>()
  for (const k of kbs) counts.set(k.name, (counts.get(k.name) ?? 0) + 1)
  const labels = new Map<number, string>()
  for (const k of kbs) {
    labels.set(k.id, (counts.get(k.name) ?? 0) > 1 ? `${k.name} ·#${k.id}` : k.name)
  }
  return labels
}
