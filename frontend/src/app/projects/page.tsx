import Link from "next/link";

import { EmptyState, Panel, Td, Th } from "@/components/primitives";
import { RelativeTime } from "@/components/relative-time";
import { SignOut } from "@/components/sign-out";
import { listProjects } from "@/lib/api";

export const metadata = { title: "Projects" };

export default async function ProjectsPage() {
  const projects = await listProjects();

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-6 flex items-baseline justify-between">
        <h1 className="text-lg font-semibold">Projects</h1>
        <SignOut />
      </header>

      <Panel>
        {projects.length === 0 ? (
          <EmptyState title="No projects yet">
            Create one with <code className="font-mono text-xs">crucible project create</code>.
          </EmptyState>
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                <Th>Project</Th>
                <Th>Slug</Th>
                <Th>Role</Th>
                <Th>Created</Th>
              </tr>
            </thead>
            <tbody>
              {projects.map((project) => (
                <tr key={project.id} className="last:[&>td]:border-b-0 hover:bg-bg">
                  <Td>
                    <Link
                      href={`/projects/${project.id}/runs`}
                      className="font-medium text-accent hover:underline"
                    >
                      {project.name}
                    </Link>
                  </Td>
                  <Td className="font-mono text-xs text-muted">{project.slug}</Td>
                  <Td className="text-muted">{project.role ?? "—"}</Td>
                  <Td className="text-muted">
                    <RelativeTime iso={project.created_at} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </main>
  );
}
