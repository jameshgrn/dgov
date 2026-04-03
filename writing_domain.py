"""Domain-specific writing knowledge: whitelists and style-aware rules."""

from __future__ import annotations

# Nouns ending in -tion/ment/ance that are standard in fluvial/sedimentology
# and should not be flagged as nominalizations.
NOMINALIZATION_WHITELIST = {
    # Process nouns
    "deposition",
    "erosion",
    "accretion",
    "avulsion",
    "aggradation",
    "degradation",
    "incision",
    "subsidence",
    "progradation",
    "retrogradation",
    "migration",
    "bifurcation",
    # Measurement/Description
    "accommodation",
    "preservation",
    "distribution",
    "concentration",
    "attenuation",
    # Standard terminology
    "formation",
    "section",
    "succession",
    "transition",
    "correlation",
    "calibration",
    "classification",
    "orientation",
    "elevation",
    "configuration",
    "stratification",
    "organization",
    "observation",
    "examination",
    "indication",
}

# Phrases that are standard scientific domain terms and should not be flagged
DOMAIN_PHRASES_WHITELIST = {
    "grain-size analysis",
    "facies association",
    "depositional environment",
    "accommodation space",
    "preservation potential",
    "sediment transport",
    "base level",
    "channel belt",
    "alluvial architecture",
    "cross-section",
    "plan-form",
    "steady-state",
    "floodplain",
}
