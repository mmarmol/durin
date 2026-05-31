import { MessageSquarePlus } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";

export function EmptyState({
  onNewChat,
}: {
  onNewChat: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
      <MessageSquarePlus
        className="h-10 w-10 text-muted-foreground"
        aria-hidden
      />
      <div className="space-y-1">
        <p className="text-lg font-medium">{t("empty.noChatsTitle")}</p>
        <p className="max-w-sm text-sm text-muted-foreground">
          {t("empty.noChatsBody")}
        </p>
      </div>
      <Button onClick={onNewChat}>{t("empty.newChat")}</Button>
    </div>
  );
}
