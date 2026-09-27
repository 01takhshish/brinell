import { redirect } from "next/navigation";

import { hasSession, listProjects } from "@/lib/api";

/**
 * There is no dashboard at the root. Everything in Crucible is scoped to a project, so the root
 * exists only to work out which project you meant and get out of the way.
 */
export default async function RootPage() {
  if (!(await hasSession())) redirect("/login");

  const projects = await listProjects();
  if (projects.length === 1) redirect(`/projects/${projects[0].id}/runs`);
  redirect("/projects");
}
