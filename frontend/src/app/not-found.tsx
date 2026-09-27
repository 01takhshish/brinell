import Link from "next/link";

export default function NotFound() {
  return (
    <main className="mx-auto max-w-lg px-4 py-20">
      <h1 className="text-lg font-semibold">Not found</h1>
      <p className="mt-2 text-sm text-muted">
        That page does not exist — or it belongs to a project this key cannot see, which Crucible
        reports the same way on purpose.
      </p>
      <Link href="/" className="mt-4 inline-block text-sm text-accent hover:underline">
        Back to projects
      </Link>
    </main>
  );
}
