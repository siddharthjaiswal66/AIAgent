"""Catalogue of every role the delivery team can staff.

`core` roles are always on the team. Other roles are staffed by the Project
Manager only when the requirements call for them.
`builder` roles implement planned development tasks.
`default_paths` are the path prefixes the role owns unless the architect
assigns additional ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RoleSpec:
    role: str
    title: str
    mission: str
    expertise: str
    core: bool = False
    builder: bool = False
    default_paths: tuple[str, ...] = field(default_factory=tuple)


ROLE_CATALOG: dict[str, RoleSpec] = {
    spec.role: spec
    for spec in [
        RoleSpec(
            "business_analyst",
            "Business Analyst",
            "Turn a raw business requirement into a complete, unambiguous, testable requirements specification.",
            "requirements elicitation, domain modelling, user stories, acceptance criteria (Gherkin), NFRs, risk analysis",
            core=True,
            default_paths=("docs/requirements/",),
        ),
        RoleSpec(
            "project_manager",
            "Project Manager",
            "Staff the right team, plan the delivery into small dependency-ordered tasks and report delivery status.",
            "agile delivery, work breakdown structures, dependency management, estimation, risk management",
            core=True,
            default_paths=("docs/plan/",),
        ),
        RoleSpec(
            "solution_architect",
            "Solution Architect",
            "Design the system architecture, technology stack, component boundaries, contracts and code ownership; guard architectural integrity.",
            "enterprise architecture, DDD, microservices/modular monoliths, REST/event-driven APIs, cloud-native design, ADRs, C4 diagrams",
            core=True,
            default_paths=("docs/architecture/",),
        ),
        RoleSpec(
            "backend_developer",
            "Senior Backend Developer",
            "Implement server-side services, business logic, APIs, persistence integration, validation, security and unit tests.",
            "clean architecture, REST APIs, ORMs, authentication/authorization, transactions, logging, unit testing",
            builder=True,
            default_paths=("backend/",),
        ),
        RoleSpec(
            "frontend_developer",
            "Senior Frontend Developer",
            "Implement the user interface, client-side state, API integration, accessibility and component tests.",
            "React/Angular/Vue, TypeScript, responsive design, accessibility (WCAG), state management, component testing",
            builder=True,
            default_paths=("frontend/",),
        ),
        RoleSpec(
            "database_engineer",
            "Database Engineer",
            "Design and implement the physical data model, migrations, indexes, seed data and data access performance.",
            "relational & NoSQL modelling, normalisation, migrations (Flyway/Liquibase/Alembic), indexing, query tuning",
            builder=True,
            default_paths=("database/",),
        ),
        RoleSpec(
            "mobile_developer",
            "Mobile Developer",
            "Implement native or cross-platform mobile applications consuming the backend APIs.",
            "React Native, Flutter, Kotlin, Swift, offline-first design, mobile security",
            builder=True,
            default_paths=("mobile/",),
        ),
        RoleSpec(
            "integration_engineer",
            "Integration Engineer",
            "Implement integrations with external systems: adapters, messaging, webhooks, ETL connectors.",
            "message brokers (Kafka/RabbitMQ), REST/SOAP clients, idempotency, retries, circuit breakers",
            builder=True,
            default_paths=("integrations/",),
        ),
        RoleSpec(
            "data_engineer",
            "Data Engineer",
            "Implement data pipelines, reporting/analytics models and batch processing.",
            "ETL/ELT, Spark, Airflow, dbt, data warehousing, data quality",
            builder=True,
            default_paths=("data/",),
        ),
        RoleSpec(
            "ml_engineer",
            "ML Engineer",
            "Implement machine learning / AI features: models, training & inference pipelines and serving.",
            "Python ML stack, LLM integration, feature engineering, model evaluation, MLOps",
            builder=True,
            default_paths=("ml/",),
        ),
        RoleSpec(
            "security_engineer",
            "Security Engineer",
            "Threat-model the design and review the implementation for vulnerabilities; define security controls.",
            "STRIDE threat modelling, OWASP Top 10/ASVS, authN/authZ, secrets management, secure SDLC",
            default_paths=("docs/security/", "security/"),
        ),
        RoleSpec(
            "qa_engineer",
            "QA / Test Engineer",
            "Own quality: test strategy, automated integration/API/E2E tests, test execution, defect reporting and sign-off.",
            "test strategy, test automation (pytest/JUnit/Jest/Playwright), API testing, requirement traceability",
            core=True,
            default_paths=("tests/", "docs/qa/"),
        ),
        RoleSpec(
            "devops_engineer",
            "DevOps Engineer",
            "Containerise, build CI/CD pipelines, infrastructure-as-code and deploy the application.",
            "Docker, docker-compose, Kubernetes/Helm, GitHub Actions/Azure DevOps, Terraform, observability",
            core=True,
            default_paths=("deploy/", "infra/", ".github/", "docker-compose.yml", "docker-compose.yaml", "Makefile", "scripts/", ".env.example"),
        ),
        RoleSpec(
            "technical_writer",
            "Technical Writer",
            "Produce complete user, developer, API and operations documentation for the delivered product.",
            "technical writing, API reference docs, runbooks, onboarding guides, diagrams as code",
            core=True,
            default_paths=("docs/", "README.md", "CHANGELOG.md"),
        ),
    ]
}

CORE_ROLES = [r for r, s in ROLE_CATALOG.items() if s.core]
BUILDER_ROLES = [r for r, s in ROLE_CATALOG.items() if s.builder]

# Paths whose ownership the architect may not reassign.
PROTECTED_PREFIXES = ("docs/", "readme.md", "changelog.md", ".agentteam")


def catalog_for_prompt() -> str:
    lines = []
    for spec in ROLE_CATALOG.values():
        tag = "ALWAYS ON TEAM" if spec.core else "optional"
        lines.append(f"- {spec.role} ({spec.title}) [{tag}]: {spec.mission}")
    return "\n".join(lines)
