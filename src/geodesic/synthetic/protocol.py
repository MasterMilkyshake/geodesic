"""Synthetic example protocol for the bundled inert-tracer fixture.

This protocol exists so the screening workflow can be demonstrated and tested.
It describes an invented engineering question about an inert tracer moving
among engineered reservoirs. It is not a template for any real research
question: GEODESIC never drafts criteria for a real project.
"""

from __future__ import annotations

from ..protocol import Criterion, DateLimits, Protocol, PublicationTypeRule, StageAutomation


def example_protocol(authorize_automation: bool = False) -> Protocol:
    """The synthetic example protocol.

    ``authorize_automation`` switches on title/abstract automation for the
    publication-type and date-limit rules, which the demonstration uses to show
    automated decisions being counted separately from human ones.
    """
    return Protocol(
        research_question=("SYNTHETIC EXAMPLE: how does an inert tracer move among the engineered reservoirs of a "
                           "test rig (release, transit, retention, discharge)?"),
        target_systems=["engineered reservoir test rigs"],
        interventions=["inert tracer dosing"],
        comparators=["undosed baseline run"],
        outcomes=["tracer concentration", "released fraction", "retained fraction", "clearance rate"],
        measurement_types=["direct concentration measurement", "optical proxy signal"],
        study_designs=["bench experiment", "pilot-rig experiment"],
        date_limits=DateLimits(from_year=2015, to_year=None),
        languages=[],
        publication_types={
            "preprints": PublicationTypeRule(treatment="flag_for_review", note="synthetic example",
                                             type_terms=["preprint"]),
            "reviews": PublicationTypeRule(treatment="exclude", note="synthetic example: used for background only",
                                           type_terms=["review"]),
            "conference_abstracts": PublicationTypeRule(treatment="flag_for_review", note="synthetic example",
                                                        type_terms=["conference abstract", "conference paper"]),
            "other": PublicationTypeRule(treatment="flag_for_review", note="synthetic example"),
        },
        inclusion_criteria=[
            Criterion(id="I1", text="Reports an experiment on an engineered reservoir rig.",
                      match_terms=["reservoir", "tank", "conduit", "weir"]),
            Criterion(id="I2", text="Uses an inert tracer.", match_terms=["tracer", "dye"]),
            Criterion(id="I3", text="Reports at least one measured quantity for a stage.",
                      applies_to=["full_text"]),
        ],
        exclusion_criteria=[
            Criterion(id="E1", text="Purely computational study with no rig measurement.",
                      match_terms=["simulation only", "purely computational"]),
            Criterion(id="E2", text="Tracer is reactive rather than inert.", match_terms=["reactive tracer"]),
        ],
        supporting_evidence_role=("SYNTHETIC EXAMPLE: results from natural (non-engineered) reservoirs are kept as "
                                  "supporting context only and are not compared with rig measurements."),
        automation={
            "title_abstract": StageAutomation(authorized=authorize_automation,
                                              allowed_rules=["publication_type", "date_limit"]
                                              if authorize_automation else []),
            "full_text": StageAutomation(authorized=False),
        },
        notes="Synthetic example protocol bundled with GEODESIC for tests and the offline demonstration.",
    )
