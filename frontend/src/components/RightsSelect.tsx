import { t } from "../i18n/sv";

export const RIGHTS = [
  "unknown",
  "user_owned",
  "licensed",
  "public_domain_verified",
  "permission_confirmed",
  "not_permitted",
];

export function RightsSelect({ value, onChange, id }: { value: string; onChange: (v: string) => void; id?: string }) {
  return (
    <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
      {RIGHTS.map((r) => (
        <option key={r} value={r}>
          {t.rights[r]}
        </option>
      ))}
    </select>
  );
}
