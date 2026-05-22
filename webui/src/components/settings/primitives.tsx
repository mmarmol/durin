import type { ReactNode } from "react";

/** Shared card chrome for every settings group. Keeping the class string
 *  in one place is what stops section styling from drifting apart — every
 *  settings section, collapsible or not, builds on this. */
export const settingsCardClass =
  "overflow-hidden rounded-[22px] border border-border/45 bg-card/86 " +
  "shadow-[0_18px_65px_rgba(15,23,42,0.075)] backdrop-blur-xl " +
  "dark:border-white/10 dark:shadow-[0_18px_65px_rgba(0,0,0,0.24)]";

export function SettingsSectionTitle({ children }: { children: ReactNode }) {
  return (
    <h2 className="mb-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
      {children}
    </h2>
  );
}

export function SettingsGroup({ children }: { children: ReactNode }) {
  return (
    <div className={settingsCardClass}>
      <div className="divide-y divide-border/45">{children}</div>
    </div>
  );
}

export function SettingsRow({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex min-h-[62px] flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <div className="min-w-0">
        <div className="text-[14px] font-medium leading-5 text-foreground">{title}</div>
        {description ? (
          <div className="mt-0.5 max-w-[28rem] text-[12px] leading-5 text-muted-foreground">
            {description}
          </div>
        ) : null}
      </div>
      {children ? <div className="shrink-0 sm:ml-6">{children}</div> : null}
    </div>
  );
}
