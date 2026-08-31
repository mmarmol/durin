import { Component, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

/** Render-error boundary around the Automations section.
 *
 *  The app has no global boundary, so before this existed a single throwing
 *  row — e.g. a malformed run record — blanked the ENTIRE app, chrome
 *  included. This contains the blast radius to the section and offers a
 *  retry, which remounts the children clean. The inner class exists only
 *  because error boundaries still require one; the translated chrome lives
 *  in the functional wrapper so the class stays hook-free. */
class Boundary extends Component<
  { title: string; retry: string; children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-8">
          <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3">
            <p className="text-[13px] font-medium text-destructive">{this.props.title}</p>
            <p className="mt-1 text-[12px] text-muted-foreground">{String(this.state.error)}</p>
            <button
              type="button"
              className="mt-2 rounded-md border border-border bg-background px-2.5 py-1 text-[12px] font-medium hover:bg-muted"
              onClick={() => this.setState({ error: null })}
            >
              {this.props.retry}
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export function SectionBoundary({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  return (
    <Boundary title={t("automations.boundary.title")} retry={t("automations.boundary.retry")}>
      {children}
    </Boundary>
  );
}
