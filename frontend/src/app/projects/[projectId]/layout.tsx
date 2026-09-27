import Link from "next/link";
import type { ReactNode } from "react";

import { SignOut } from "@/components/sign-out";
import { getProject } from "@/lib/api";

export default async function ProjectLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = await params;
  const project = await getProject(projectId);

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 border-b bg-panel/85 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-2.5">
          <Link href={`/projects/${projectId}/runs`} className="text-sm font-semibold">
            Crucible
          </Link>
          <span className="text-faint">/</span>
          <Link href="/projects" className="text-sm text-muted hover:text-text hover:underline">
            {project.name}
          </Link>
          <div className="ml-auto">
            <SignOut />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
    </div>
  );
}
