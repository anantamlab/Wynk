import { Compass } from "@phosphor-icons/react";
import { lazy, Suspense } from "react";
import { Link, Navigate, type RouteObject, useRouteError } from "react-router";
import { AppShell } from "@/components/app/AppShell";
import { ExperimentGate } from "@/components/app/ExperimentGate";
import { EmptyState, ErrorState } from "@/components/app/ui";
import { ConfigurePage } from "@/pages/ConfigurePage";
import { DatasetDetailPage } from "@/pages/DatasetDetailPage";
import { DatasetImportPage } from "@/pages/DatasetImportPage";
import { DatasetsPage } from "@/pages/DatasetsPage";
import { ExperimentPage } from "@/pages/ExperimentPage";
import { ExperimentsPage } from "@/pages/ExperimentsPage";
import { ProjectLayout } from "@/pages/ProjectLayout";
import { ProjectsPage } from "@/pages/ProjectsPage";
import { ResultsPage } from "@/pages/ResultsPage";
import { WorkflowsPage } from "@/pages/WorkflowsPage";
import { LiveConfigurePage } from "@/pages/live/LiveConfigurePage";
import { LiveExperimentPage } from "@/pages/live/LiveExperimentPage";
import { LiveExperimentsPage } from "@/pages/live/LiveExperimentsPage";
import { LiveResultsPage } from "@/pages/live/LiveResultsPage";
import { LiveWorkflowsPage } from "@/pages/live/LiveWorkflowsPage";

// the hackathon chat demo, kept whole; loaded only when visited
const Playground = lazy(() => import("./Playground"));

/*
  Project → Dataset → Configure → Optimize → Compare → Champion

  /projects
  /projects/:projectId/datasets            list
  /projects/:projectId/datasets/new        upload, server inspection, column roles, register (?upload=)
  /projects/:projectId/datasets/:id        versions, schema and roles, splits, preview (?version=)
  /projects/:projectId/experiments         list
  /projects/:projectId/experiments/new     configure
  /projects/:projectId/experiments/:id     run: progress, curve, candidates, search
  /projects/:projectId/experiments/:id/results   baseline vs random vs Wynk, champion, why
  /projects/:projectId/workflows           validated workflows and champions
  /playground                              ask-a-question chat demo

  Experiment screens sit behind ExperimentGate: with the live API (the default) they are the
  `pages/live/` screens over the product API's real experiment, promotion, deployment and
  inference routes; with the mock they are the simulated demo, labelled as simulated.
*/
const gated = (mock: React.ReactNode, live: React.ReactNode) => <ExperimentGate live={live}>{mock}</ExperimentGate>;
export const routes: RouteObject[] = [
  {
    path: "/playground",
    element: (
      <Suspense fallback={null}>
        <Playground />
      </Suspense>
    ),
  },
  {
    element: <AppShell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/projects" replace /> },
      { path: "projects", element: <ProjectsPage /> },
      {
        path: "projects/:projectId",
        element: <ProjectLayout />,
        children: [
          { index: true, element: <Navigate to="datasets" replace /> },
          { path: "datasets", element: <DatasetsPage /> },
          { path: "datasets/new", element: <DatasetImportPage /> },
          { path: "datasets/:datasetId", element: <DatasetDetailPage /> },
          { path: "experiments", element: gated(<ExperimentsPage />, <LiveExperimentsPage />) },
          { path: "experiments/new", element: gated(<ConfigurePage />, <LiveConfigurePage />) },
          { path: "experiments/:experimentId", element: gated(<ExperimentPage />, <LiveExperimentPage />) },
          { path: "experiments/:experimentId/results", element: gated(<ResultsPage />, <LiveResultsPage />) },
          { path: "workflows", element: gated(<WorkflowsPage />, <LiveWorkflowsPage />) },
        ],
      },
      { path: "*", element: <NotFound /> },
    ],
  },
];

function NotFound() {
  return (
    <EmptyState
      icon={Compass}
      title="Nothing here"
      action={
        <Link to="/projects" className="btn btn-primary btn-sm">
          Go to projects
        </Link>
      }
    >
      This address does not match a page.
    </EmptyState>
  );
}

function RouteError() {
  const error = useRouteError();
  return (
    <div className="mx-auto max-w-2xl p-8">
      <ErrorState title="Something broke on this page" message={error instanceof Error ? error.message : "An unexpected error occurred."} />
    </div>
  );
}
