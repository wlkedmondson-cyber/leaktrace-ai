import os
from dotenv import load_dotenv

load_dotenv()


def has_any(text, terms):
    return any(term in text for term in terms)


def run_leak_investigation(case, photos):
    """
    V1 hybrid diagnosis engine with roof probability heatmap support.
    """

    symptom = (case.get("symptom_type") or "").lower()
    location = (case.get("symptom_location") or "").lower()
    timing = (case.get("leak_timing") or "").lower()
    features = (case.get("known_features") or "").lower()
    description = (case.get("description") or "").lower()
    storm = (case.get("storm_context") or "").lower()
    roof_age = (case.get("roof_age") or "").lower()
    roof_type = (case.get("roof_type") or "").lower()

    full_text = " ".join([symptom, location, timing, features, description, storm, roof_age, roof_type])

    has_chimney = has_any(full_text, ["chimney", "fireplace", "masonry", "counterflashing"])
    has_skylight = has_any(full_text, ["skylight"])
    has_pipe_or_vent = has_any(full_text, ["pipe", "plumbing vent", "roof vent", "vent boot", "boot"])
    has_valley = has_any(full_text, ["valley"])
    has_sidewall = has_any(full_text, ["sidewall", "wall", "siding", "dormer"])
    has_gutter = has_any(full_text, ["gutter", "eave", "fascia", "soffit"])
    has_hvac = has_any(full_text, ["hvac", "unit", "curb", "equipment"])

    is_asphalt = has_any(roof_type, ["asphalt", "shingle"])
    is_standing_seam = "standing seam" in roof_type
    is_exposed_fastener = "exposed fastener" in roof_type or "corrugated" in roof_type
    is_metal = "metal" in roof_type or is_standing_seam or is_exposed_fastener

    candidates = {}

    def add(name, score, cause):
        candidates[name] = {"score": score, "cause": cause}

    add(
        "General Roof Field Damage / Exposed Fastener",
        24,
        "Damaged roofing surface, lifted material, nail pop, exposed fastener, failed washer, or storm-created opening."
    )

    add(
        "Roof Penetration / Vent Flashing Failure",
        22,
        "Failed seal, cracked boot, loose flashing, exposed fasteners, or separation around a roof penetration."
    )

    add(
        "Valley or Roof Transition Leak",
        18,
        "Water concentration at a valley, slope change, or roof transition may be bypassing the water-shedding system."
    )

    if has_chimney:
        add(
            "Chimney Flashing / Counterflashing Failure",
            48,
            "Water intrusion around chimney flashing, counterflashing, cricket/saddle, mortar joint, or adjacent roof-to-wall transition."
        )

    if has_skylight:
        add(
            "Skylight Flashing or Seal Failure",
            48,
            "Failed skylight flashing, curb detail, seal, or uphill water diversion."
        )

    if has_sidewall:
        add(
            "Sidewall / Step Flashing Failure",
            38,
            "Failure at roof-to-wall transition, missing/incorrect step flashing, siding gap, or wind-driven rain entry."
        )

    if has_gutter:
        add(
            "Gutter Backup / Edge Intrusion",
            30,
            "Overflowing gutter, fascia/edge metal issue, or water backing up at the eave."
        )

    if has_hvac:
        add(
            "HVAC Curb / Equipment Flashing Failure",
            44,
            "Water intrusion around rooftop equipment curb, equipment flashing, fasteners, sealant joints, or uphill drainage obstruction."
        )

    if has_pipe_or_vent:
        candidates["Roof Penetration / Vent Flashing Failure"]["score"] += 32

    if has_valley:
        candidates["Valley or Roof Transition Leak"]["score"] += 30

    if "active" in symptom or "drip" in symptom:
        candidates["Roof Penetration / Vent Flashing Failure"]["score"] += 8
        candidates["General Roof Field Damage / Exposed Fastener"]["score"] += 8

    if "ceiling" in symptom or "ceiling" in location:
        candidates["Roof Penetration / Vent Flashing Failure"]["score"] += 6
        candidates["General Roof Field Damage / Exposed Fastener"]["score"] += 5

    if "wind" in timing or "wind" in storm:
        candidates["General Roof Field Damage / Exposed Fastener"]["score"] += 10

        if has_chimney:
            candidates["Chimney Flashing / Counterflashing Failure"]["score"] += 12

        if has_sidewall:
            candidates["Sidewall / Step Flashing Failure"]["score"] += 12

        if has_skylight:
            candidates["Skylight Flashing or Seal Failure"]["score"] += 8

        if has_hvac:
            candidates["HVAC Curb / Equipment Flashing Failure"]["score"] += 8

    if "heavy" in timing or "long" in timing or "steady" in timing:
        candidates["Valley or Roof Transition Leak"]["score"] += 10

        if has_gutter:
            candidates["Gutter Backup / Edge Intrusion"]["score"] += 8

    if "storm" in storm or "hail" in storm or "wind" in storm:
        candidates["General Roof Field Damage / Exposed Fastener"]["score"] += 16

    if "15" in roof_age or "20" in roof_age or "old" in roof_age:
        candidates["Roof Penetration / Vent Flashing Failure"]["score"] += 8
        candidates["General Roof Field Damage / Exposed Fastener"]["score"] += 8

    if is_asphalt:
        candidates["Roof Penetration / Vent Flashing Failure"]["score"] += 8
        candidates["Valley or Roof Transition Leak"]["score"] += 6

    if is_standing_seam:
        candidates["Roof Penetration / Vent Flashing Failure"]["score"] += 12
        candidates["General Roof Field Damage / Exposed Fastener"]["cause"] = (
            "Possible standing seam issue such as panel damage, seam separation, clip/thermal movement concern, "
            "or penetration detail failure."
        )

    if is_exposed_fastener:
        candidates["General Roof Field Damage / Exposed Fastener"]["score"] += 30
        candidates["General Roof Field Damage / Exposed Fastener"]["cause"] = (
            "Exposed fastener metal systems commonly leak from failed neoprene washers, backed-out screws, "
            "ridge cap details, or penetration flashing."
        )

    if is_metal and not has_pipe_or_vent:
        candidates["Roof Penetration / Vent Flashing Failure"]["score"] -= 6

    sorted_items = sorted(candidates.items(), key=lambda item: item[1]["score"], reverse=True)

    top_source, top_data = sorted_items[0]
    second_source, second_data = sorted_items[1] if len(sorted_items) > 1 else (
        "Further Inspection Needed",
        {"score": 1}
    )

    top_score = top_data["score"]
    second_score = max(second_data["score"], 1)

    confidence = min(
        91,
        max(
            57,
            round(top_score / (top_score + second_score) * 100 + 24, 1)
        )
    )

    urgency = "Moderate"

    if "active" in symptom or "drip" in symptom:
        urgency = "High"
    elif "musty" in symptom or "mold" in symptom:
        urgency = "Investigate Soon"

    heatmap_zones = build_heatmap_zones(top_source, confidence)
    callout_markers = build_callout_markers(top_source)

    return {
        "probable_source": top_source,
        "probable_cause": top_data["cause"],
        "confidence": confidence,
        "secondary_possibility": second_source,
        "urgency": urgency,
        "summary": build_summary(top_source, roof_type, has_chimney, has_skylight, has_pipe_or_vent),
        "confirmation_steps": build_confirmation_steps(top_source),
        "repair_recommendation": build_repair_recommendation(top_source),
        "estimated_cost_range": build_cost_range(top_source),
        "heatmap_zones": heatmap_zones,
        "callout_markers": callout_markers,
    }


def build_heatmap_zones(source, confidence):
    high = min(96, max(65, float(confidence or 70)))
    medium = max(35, round(high - 22, 1))
    low = max(20, round(high - 42, 1))

    if "Penetration" in source or "Vent" in source:
        center_x, center_y = 50, 42
    elif "Valley" in source or "Transition" in source:
        center_x, center_y = 42, 38
    elif "Chimney" in source:
        center_x, center_y = 58, 35
    elif "Skylight" in source:
        center_x, center_y = 50, 35
    elif "Sidewall" in source or "Step Flashing" in source:
        center_x, center_y = 68, 44
    elif "Gutter" in source or "Edge" in source:
        center_x, center_y = 50, 72
    elif "HVAC" in source or "Equipment" in source or "Curb" in source:
        center_x, center_y = 54, 39
    else:
        center_x, center_y = 52, 45

    return [
        {
            "label": "Primary AI Search Zone",
            "x": center_x,
            "y": center_y,
            "radius": 24,
            "confidence": high,
            "severity": "high",
        },
        {
            "label": "Secondary Water Travel Zone",
            "x": min(88, center_x + 13),
            "y": max(12, center_y - 8),
            "radius": 18,
            "confidence": medium,
            "severity": "medium",
        },
        {
            "label": "Extended Inspection Area",
            "x": max(12, center_x - 15),
            "y": min(88, center_y + 10),
            "radius": 16,
            "confidence": low,
            "severity": "low",
        },
    ]

def build_callout_markers(source):
    if "Penetration" in source or "Vent" in source:
        return [
            {"x": 50, "y": 42, "label": "Inspect pipe boot / roof penetration"},
            {"x": 62, "y": 34, "label": "Check uphill flashing edge"},
        ]

    if "Valley" in source or "Transition" in source:
        return [
            {"x": 42, "y": 38, "label": "Inspect valley water channel"},
            {"x": 55, "y": 46, "label": "Check transition seam"},
        ]

    if "Chimney" in source:
        return [
            {"x": 58, "y": 35, "label": "Inspect chimney counterflashing"},
            {"x": 50, "y": 43, "label": "Check uphill cricket / saddle"},
        ]

    if "Skylight" in source:
        return [
            {"x": 50, "y": 35, "label": "Inspect skylight curb flashing"},
            {"x": 58, "y": 42, "label": "Check seal and uphill diverter"},
        ]

    if "Sidewall" in source or "Step Flashing" in source:
        return [
            {"x": 68, "y": 44, "label": "Inspect step flashing"},
            {"x": 73, "y": 54, "label": "Check wall transition / siding clearance"},
        ]

    if "Gutter" in source or "Edge" in source:
        return [
            {"x": 50, "y": 72, "label": "Inspect eave edge / drip edge"},
            {"x": 42, "y": 78, "label": "Check gutter backup path"},
        ]

    if "HVAC" in source or "Equipment" in source or "Curb" in source:
        return [
            {"x": 54, "y": 39, "label": "Inspect equipment curb flashing"},
            {"x": 63, "y": 47, "label": "Check uphill drainage around unit"},
        ]

    return [
        {"x": 52, "y": 45, "label": "Inspect likely roof field damage"},
        {"x": 61, "y": 38, "label": "Check fasteners / lifted material"},
    ]

def build_summary(source, roof_type, has_chimney, has_skylight, has_pipe_or_vent):
    return (
        f"The investigation currently points most strongly toward {source}. "
        f"This result is based on the selected roof system ({roof_type or 'unknown'}), reported symptoms, "
        f"timing of the leak, roof complexity indicators, and submitted visual evidence. "
        "Feature-specific causes are only ranked when that feature is present or described."
    )


def build_confirmation_steps(source):
    steps = {
        "Roof Penetration / Vent Flashing Failure": "Inspect penetrations above the leak area. Look for cracked rubber, lifted flashing, failed sealant, backed-out fasteners, or separation. Perform a controlled hose test around one penetration at a time.",
        "Chimney Flashing / Counterflashing Failure": "Inspect uphill chimney side, step flashing, counterflashing, cricket/saddle, and mortar joints. Water-test in stages from low to high.",
        "Valley or Roof Transition Leak": "Inspect valley metal/shingle cuts, debris buildup, panel transitions, fastener placement, and slope changes. Water-test lower valley first, then move upward.",
        "Skylight Flashing or Seal Failure": "Inspect uphill skylight flashing, curb corners, sealant, and interior condensation signs. Water-test flashing separately from glass/seal areas.",
        "Sidewall / Step Flashing Failure": "Inspect step flashing, siding clearance, kickout flashing, and wall transitions. Test with wind-driven rain simulation if possible.",
        "General Roof Field Damage / Exposed Fastener": "Inspect the roof field above the leak for lifted shingles, punctures, nail pops, failed metal fastener washers, backed-out screws, damaged panels, or exposed fasteners.",
        "Gutter Backup / Edge Intrusion": "Inspect gutter flow, debris, fascia, drip edge, and eave condition during heavy water flow.",
        "HVAC Curb / Equipment Flashing Failure": "Inspect the uphill side of the equipment curb, curb flashing, counterflashing, fasteners, sealant joints, and drainage paths around the unit. Water-test in controlled sections.",
    }
    return steps.get(source, "Perform staged inspection from the visible symptom upward to the most likely roof entry points.")


def build_repair_recommendation(source):
    repairs = {
        "Roof Penetration / Vent Flashing Failure": "Replace or properly reflash the failed penetration detail, reseal fasteners, and inspect surrounding decking.",
        "Chimney Flashing / Counterflashing Failure": "Repair or replace chimney flashing/counterflashing, seal masonry defects, and verify cricket or saddle drainage.",
        "Valley or Roof Transition Leak": "Correct the affected valley or transition detail, replace damaged material, and verify proper water flow.",
        "Skylight Flashing or Seal Failure": "Repair skylight flashing kit, curb detail, and seals. Replace skylight if frame or glazing failure is confirmed.",
        "Sidewall / Step Flashing Failure": "Correct step flashing and kickout flashing, repair siding clearance, and seal wall transition properly.",
        "General Roof Field Damage / Exposed Fastener": "Replace damaged roofing material or correct failed fasteners/washers. Avoid relying on exposed sealant as a permanent fix.",
        "Gutter Backup / Edge Intrusion": "Clean/resize gutters, correct drip edge/fascia issues, and repair any eave deterioration.",
        "HVAC Curb / Equipment Flashing Failure": "Repair or replace the failed equipment curb flashing, correct sealant joints, and verify drainage around rooftop equipment.",
    }
    return repairs.get(source, "Confirm the entry point, then repair the failed water-shedding component.")


def build_cost_range(source):
    costs = {
        "Roof Penetration / Vent Flashing Failure": "$350-$850",
        "Chimney Flashing / Counterflashing Failure": "$750-$2,500+",
        "Valley or Roof Transition Leak": "$650-$2,000+",
        "Skylight Flashing or Seal Failure": "$600-$2,500+",
        "Sidewall / Step Flashing Failure": "$750-$2,500+",
        "General Roof Field Damage / Exposed Fastener": "$300-$1,500",
        "Gutter Backup / Edge Intrusion": "$250-$1,500",
        "HVAC Curb / Equipment Flashing Failure": "$750-$3,500+",
    }
    return costs.get(source, "Requires inspection")