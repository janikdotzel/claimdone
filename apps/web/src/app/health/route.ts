const HEALTH_PAYLOAD = {
  service: "web",
  status: "ok",
} as const;

export function GET(): Response {
  return Response.json(HEALTH_PAYLOAD, { status: 200 });
}
