import { Theme, updateSettings, useSettings } from "../lib/settings";

const OPTIONS: { value: Theme; label: string }[] = [
  { value: "system", label: "Auto" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

export function ThemeSwitch() {
  const { theme } = useSettings();
  return (
    <div role="group" aria-label="Color theme" className="seg">
      {OPTIONS.map((o) => (
        <button key={o.value} type="button" aria-pressed={theme === o.value} onClick={() => updateSettings({ theme: o.value })}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
