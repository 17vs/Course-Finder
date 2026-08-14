import json
import os
import re
import uuid

import requests
from bs4 import BeautifulSoup
from graphviz import Digraph
from flask import Flask, request, render_template, redirect, session


app = Flask(__name__, static_folder="static")

# Flask's default session is a signed browser cookie. Only small,
# user-specific selections are stored in it.
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "course-finder-local-development-only"
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(
    os.environ.get("RENDER")
)


CATALOG_DIR = "catalog"
MAJORS_FILE = "majors.json"
COURSE_INDEX_FILE = "course_index.json"

os.makedirs(CATALOG_DIR, exist_ok=True)
os.makedirs("static", exist_ok=True)


with open(MAJORS_FILE, "r", encoding="utf-8") as file:
    majors = json.load(file)


def load_course_index():
    if not os.path.exists(COURSE_INDEX_FILE):
        return {}

    with open(COURSE_INDEX_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


course_index = load_course_index()


# -------------------------
# Shared catalog cache
# -------------------------
# These values contain public UF catalog data and are safe to share
# within a Gunicorn worker. User selections are stored in Flask session.
course_data = {}
loaded_catalogs = set()


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def normalize_course_code(course_code):
    """
    UF catalog pages frequently use non-breaking spaces between
    the subject prefix and course number (for example MAC\xa02312).

    Normalize all whitespace so course codes always use a normal
    single ASCII space, matching the keys in course_index.json.
    """
    if not course_code:
        return ""

    return " ".join(
        str(course_code)
        .replace("\xa0", " ")
        .strip()
        .upper()
        .split()
    )


def catalog_path(subject_name):
    return os.path.join(
        CATALOG_DIR,
        f"UF_{safe_name(subject_name)}_Catalog.html"
    )


def download_catalog(subject_name):
    """
    Return the local catalog path.
    If it is not already cached in catalog/, download it first.
    """
    if subject_name not in majors:
        return None

    path = catalog_path(subject_name)

    if os.path.exists(path):
        return path

    response = requests.get(
        majors[subject_name],
        timeout=30,
        headers={"User-Agent": "Course-Finder/1.0"}
    )
    response.raise_for_status()

    with open(path, "w", encoding="utf-8") as file:
        file.write(response.text)

    return path


def find_course_names(text):
    """
    Extract and normalize course codes such as:
    MAC 2312
    EEL 3111C
    COP 3504C
    """
    pattern = r"\b[A-Z]{2,4}\s+\d{4}[A-Z]?\b"

    matches = re.findall(pattern, text)

    return list(
        dict.fromkeys(
            normalize_course_code(code)
            for code in matches
        )
    )



def parse_prerequisite_groups(text):
    """
    Convert UF prerequisite text into groups.

    Each inner list is an OR group.
    Multiple groups are ANDed together.

    Examples:

        "MAC 2312 and PHY 2049"
        ->
        [["MAC 2312"], ["PHY 2049"]]

        "MAC 2313 or MAC 3474"
        ->
        [["MAC 2313", "MAC 3474"]]

        "(MAC 2313 or MAC 3474) and (MAS 3300 or MHF 3202)"
        ->
        [
            ["MAC 2313", "MAC 3474"],
            ["MAS 3300", "MHF 3202"]
        ]

        "MAC 2312, MAC 2512, or MAC 3473"
        ->
        [["MAC 2312", "MAC 2512", "MAC 3473"]]
    """
    if not text:
        return []

    normalized = (
        text.replace("\xa0", " ")
        .replace(";", " ")
    )

    # Only analyze the part of the sentence spanning from the first
    # course code through the last course code. This avoids trailing
    # prose such as "with a minimum grade of C".
    course_matches = list(
        re.finditer(
            r"\b[A-Z]{2,4}\s+\d{4}[A-Z]?\b",
            normalized
        )
    )

    if not course_matches:
        return []

    relevant = normalized[
        course_matches[0].start():
        course_matches[-1].end()
    ]

    # Remove "Prerequisite:" if it happened to survive into the span.
    relevant = re.sub(
        r"^Prerequisites?:\s*",
        "",
        relevant,
        flags=re.IGNORECASE
    )

    def split_top_level_and(expression):
        """
        Split only on AND operators outside parentheses.
        """
        parts = []
        start = 0
        depth = 0

        token_pattern = re.compile(
            r"\(|\)|\band\b",
            re.IGNORECASE
        )

        for match in token_pattern.finditer(expression):
            token = match.group(0).lower()

            if token == "(":
                depth += 1

            elif token == ")":
                depth = max(0, depth - 1)

            elif token == "and" and depth == 0:
                parts.append(
                    expression[start:match.start()].strip(" ,")
                )
                start = match.end()

        parts.append(
            expression[start:].strip(" ,")
        )

        return [part for part in parts if part]

    def strip_outer_parentheses(expression):
        expression = expression.strip()

        while (
            len(expression) >= 2
            and expression.startswith("(")
            and expression.endswith(")")
        ):
            depth = 0
            wraps_entire_expression = True

            for index, character in enumerate(expression):
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1

                    if depth == 0 and index != len(expression) - 1:
                        wraps_entire_expression = False
                        break

            if not wraps_entire_expression:
                break

            expression = expression[1:-1].strip()

        return expression

    groups = []

    top_level_parts = split_top_level_and(relevant)

    for part in top_level_parts:
        part = strip_outer_parentheses(part)

        # A part may still contain parenthesized subexpressions.
        # If so, parse those separately.
        parenthetical_parts = re.findall(
            r"\(([^()]*)\)",
            part
        )

        if parenthetical_parts:
            consumed_courses = set()

            for inner in parenthetical_parts:
                inner_courses = find_course_names(inner)

                if not inner_courses:
                    continue

                consumed_courses.update(inner_courses)

                if re.search(r"\bor\b", inner, re.IGNORECASE):
                    groups.append(inner_courses)
                else:
                    groups.extend([[course] for course in inner_courses])

            remaining_courses = [
                course
                for course in find_course_names(part)
                if course not in consumed_courses
            ]

            if remaining_courses:
                remaining_text = part

                if (
                    re.search(r"\bor\b", remaining_text, re.IGNORECASE)
                    and not re.search(r"\band\b", remaining_text, re.IGNORECASE)
                ):
                    groups.append(remaining_courses)
                else:
                    groups.extend(
                        [[course] for course in remaining_courses]
                    )

            continue

        part_courses = find_course_names(part)

        if not part_courses:
            continue

        # If this clause contains OR and no AND, treat every course in
        # the clause as alternatives. This also handles Oxford-comma
        # constructions such as "A, B, or C".
        if (
            re.search(r"\bor\b", part, re.IGNORECASE)
            and not re.search(r"\band\b", part, re.IGNORECASE)
        ):
            groups.append(part_courses)

        else:
            groups.extend(
                [[course] for course in part_courses]
            )

    # Deduplicate both within groups and across identical groups.
    cleaned_groups = []
    seen_groups = set()

    for group in groups:
        cleaned_group = []

        for course in group:
            course = normalize_course_code(course)

            if course and course not in cleaned_group:
                cleaned_group.append(course)

        if not cleaned_group:
            continue

        group_key = tuple(cleaned_group)

        if group_key not in seen_groups:
            cleaned_groups.append(cleaned_group)
            seen_groups.add(group_key)

    return cleaned_groups


def flatten_prerequisite_groups(groups):
    """
    Return every prerequisite course exactly once while preserving order.
    """
    flattened = []

    for group in groups:
        for course in group:
            if course not in flattened:
                flattened.append(course)

    return flattened

def parse_catalog(subject_name):
    """
    Parse one UF subject catalog and merge its courses into course_data.
    Returns the course codes found in this catalog.
    """
    if subject_name in loaded_catalogs:
        return [
            code
            for code, data in course_data.items()
            if data["subject"] == subject_name
        ]

    path = download_catalog(subject_name)

    if not path:
        return []

    with open(path, "r", encoding="utf-8") as file:
        soup = BeautifulSoup(file, "html.parser")

    subject_courses = []

    for block in soup.find_all(
        "div",
        class_="courseblock courseblocktoggle"
    ):
        title_element = block.find(class_="courseblocktitle")

        if not title_element:
            continue

        title_text = title_element.get_text(" ", strip=True)

        match = re.search(
            r"\b([A-Z]{2,4})\s+(\d{4}[A-Z]?)\b",
            title_text
        )

        if not match:
            continue

        course_code = normalize_course_code(
            f"{match.group(1)} {match.group(2)}"
        )
        subject_courses.append(course_code)

        credits_element = title_element.find(
            "span",
            class_="credits"
        )

        credits = (
            credits_element.get_text(" ", strip=True)
            if credits_element
            else ""
        )

        # Permanently clean the course title BEFORE storing it.
        #
        # UF can use non-breaking spaces and can repeat the course code
        # in the title markup. Normalize whitespace first, remove the
        # credit text, then repeatedly strip the course code from the
        # beginning until it is gone.
        course_name = " ".join(
            title_text
            .replace("\xa0", " ")
            .split()
        )

        if credits:
            normalized_credits = " ".join(
                credits
                .replace("\xa0", " ")
                .split()
            )

            course_name = course_name.replace(
                normalized_credits,
                ""
            ).strip()

        # Example:
        # "EEL 3111C EEL 3111C Circuits 1"
        # becomes:
        # "Circuits 1"
        while True:
            title_parts = course_name.split()

            if len(title_parts) < 2:
                break

            beginning = normalize_course_code(
                " ".join(title_parts[:2])
            )

            if beginning != course_code:
                break

            course_name = " ".join(
                title_parts[2:]
            ).strip()

        # Remove separators that might remain after stripping the code.
        course_name = re.sub(
            r"^[\s|\-–—:]+",
            "",
            course_name
        ).strip()

        description_element = block.find(
            class_="courseblockdesc"
        )

        description = (
            description_element.get_text(" ", strip=True)
            if description_element
            else ""
        )

        prereq_groups = []
        coreqs = []

        for extra in block.find_all(class_="courseblockextra"):
            extra_text = extra.get_text(" ", strip=True)
            normalized = extra_text.lower()

            if normalized.startswith("prerequisite"):
                prereq_groups.extend(
                    parse_prerequisite_groups(extra_text)
                )

            elif normalized.startswith("corequisite"):
                coreqs.extend(
                    find_course_names(extra_text)
                )

        # Keep a flat list too. It is useful for dropdowns and
        # backwards-compatible iteration, while prereq_groups preserves
        # the actual AND/OR meaning.
        prereqs = flatten_prerequisite_groups(
            prereq_groups
        )

        coreqs = list(dict.fromkeys(coreqs))

        course_data[course_code] = {
            "subject": subject_name,
            "name": course_name,
            "credits": credits,
            "description": description,
            "prereqs": prereqs,
            "prereq_groups": prereq_groups,
            "coreqs": coreqs,
        }

        # This also lets the app learn course locations even if the
        # prebuilt index was missing an entry.
        course_index.setdefault(course_code, subject_name)

    loaded_catalogs.add(subject_name)

    return subject_courses


def load_course(course_code):
    """
    Ensure a course from any subject is available in course_data.

    If MAC 2312 is encountered while an ECE course is selected, this function:
      1. normalizes the course code,
      2. looks up MAC 2312 in course_index.json,
      3. finds Mathematics,
      4. loads the Mathematics catalog,
      5. makes MAC 2312 and its prerequisites available.
    """
    course_code = normalize_course_code(course_code)

    if course_code in course_data:
        return True

    subject_name = course_index.get(course_code)

    if not subject_name:
        return False

    parse_catalog(subject_name)

    return course_code in course_data


def save_course_index():
    """
    Persist any course locations learned while the app was running.
    This is optional, but it makes the index gradually more complete.
    """
    with open(COURSE_INDEX_FILE, "w", encoding="utf-8") as file:
        json.dump(
            course_index,
            file,
            indent=2,
            ensure_ascii=False,
            sort_keys=True
        )


def get_session_id():
    """
    Give each browser session a stable ID used only for generated
    graph filenames. This prevents different users from overwriting
    each other's images.
    """
    if "session_id" not in session:
        session["session_id"] = uuid.uuid4().hex

    return session["session_id"]


def current_major():
    return session.get("major", "")


def current_course():
    return session.get("course_wanted", "")


def current_graph_depth():
    return int(session.get("graph_depth", 3))


def current_max_graph_depth():
    return int(session.get("max_graph_depth", 1))


def get_current_courses():
    major_name = current_major()

    if not major_name:
        return []

    return parse_catalog(major_name)


def get_all_coreqs(course_code):
    """
    Corequisite relationships are effectively mutual for display.
    Include both corequisites explicitly listed by this course and
    reverse relationships where another loaded course lists this one.
    """
    course_code = normalize_course_code(course_code)

    load_course(course_code)

    coreqs = []

    if course_code in course_data:
        for coreq in course_data[course_code].get("coreqs", []):
            coreq = normalize_course_code(coreq)

            if coreq and coreq not in coreqs:
                coreqs.append(coreq)

    for other_course, data in course_data.items():
        other_coreqs = [
            normalize_course_code(coreq)
            for coreq in data.get("coreqs", [])
        ]

        if (
            course_code in other_coreqs
            and other_course != course_code
            and other_course not in coreqs
        ):
            coreqs.append(other_course)

    return coreqs


def toPrint(course_code):
    course_code = normalize_course_code(course_code)

    load_course(course_code)

    if course_code not in course_data:
        return course_code

    data = course_data[course_code]

    first_line = course_code

    if data["name"]:
        first_line += f" - {data['name']}"

    if data["credits"]:
        first_line += f" | {data['credits']}"

    pieces = [f"<b><u>{first_line}</u></b>"]

    if data["description"]:
        pieces.append(data["description"])

    prereq_groups = data.get(
        "prereq_groups",
        [[course] for course in data.get("prereqs", [])]
    )

    if prereq_groups:
        readable_groups = []

        for group in prereq_groups:
            if len(group) == 1:
                readable_groups.append(group[0])
            else:
                readable_groups.append(
                    "(" + " OR ".join(group) + ")"
                )

        pieces.append(
            "<b>Prerequisites:</b><br>" +
            " AND ".join(readable_groups)
        )

    all_coreqs = get_all_coreqs(course_code)

    if all_coreqs:
        pieces.append(
            "<b>Corequisites:</b><br>" +
            ", ".join(all_coreqs)
        )

    return "<br>".join(pieces)



def get_selectable_depth_max(max_depth=None):
    """
    The calculated tree depth includes the selected/root course level.
    The UI selector represents prerequisite generations only.
    """
    if max_depth is None:
        max_depth = current_max_graph_depth()

    return max(1, int(max_depth) - 1)


def template_data(**extra):
    major_name = current_major()
    course_wanted = current_course()
    graph_depth = current_graph_depth()
    max_graph_depth = current_max_graph_depth()

    data = {
        "courses": get_current_courses(),
        "major": major_name,
        "majors": majors,
        "selected_major": major_name,
        "course_wanted": course_wanted,
        "graph_depth": graph_depth,
        "max_graph_depth": max_graph_depth,
        "max_selectable_depth": get_selectable_depth_max(
            max_graph_depth
        ),
    }

    data.update(extra)
    return data


@app.route("/")
@app.route("/home")
def home():
    return render_template(
        "index.html",
        **template_data()
    )


@app.route("/reset")
def reset():
    session.clear()
    return redirect("/")


@app.route("/major", methods=["POST"])
def choose_major():
    selected_major = request.form.get(
        "major",
        ""
    ).strip()

    if selected_major == "":
        session.clear()
        return redirect("/")

    if selected_major not in majors:
        return redirect("/")

    # Load/cache the public catalog data, then store only the user's
    # current selection in the session.
    parse_catalog(selected_major)

    session["major"] = selected_major
    session["course_wanted"] = ""
    session["graph_depth"] = 3
    session["max_graph_depth"] = 1
    session["selected_current"] = ""

    get_session_id()

    save_course_index()

    return redirect("/")


def normalize_prereq_group(group):
    """
    Normalize one OR group and remove duplicates/self-empty values.
    The returned tuple is sorted so logically identical groups have
    exactly the same key regardless of catalog ordering.
    """
    normalized = {
        normalize_course_code(course)
        for course in group
        if normalize_course_code(course)
    }

    return tuple(sorted(normalized))


def calculate_max_graph_depth(root_course):
    """
    Calculate the maximum dependency depth for the selected course.

    A depth of 1 means the course has only immediate dependencies visible
    at the first level. Deeper prerequisite/corequisite chains increase
    this value.

    Cycles are ignored so malformed or reciprocal catalog relationships
    cannot recurse forever.
    """
    root_course = normalize_course_code(root_course)

    if not root_course:
        return 1

    memo = {}

    def depth_from(course_code, visiting):
        course_code = normalize_course_code(course_code)

        if not course_code:
            return 0

        if course_code in memo:
            return memo[course_code]

        if course_code in visiting:
            return 0

        if not load_course(course_code):
            memo[course_code] = 0
            return 0

        visiting.add(course_code)

        data = course_data[course_code]

        dependencies = []

        prereq_groups = data.get(
            "prereq_groups",
            [[course] for course in data.get("prereqs", [])]
        )

        for group in prereq_groups:
            for prereq in group:
                prereq = normalize_course_code(prereq)

                if prereq and prereq != course_code:
                    dependencies.append(prereq)

        for coreq in data.get("coreqs", []):
            coreq = normalize_course_code(coreq)

            if coreq and coreq != course_code:
                dependencies.append(coreq)

        dependencies = list(dict.fromkeys(dependencies))

        if not dependencies:
            result = 0
        else:
            result = 1 + max(
                depth_from(dependency, visiting)
                for dependency in dependencies
            )

        visiting.discard(course_code)
        memo[course_code] = result

        return result

    # Keep at least one selectable level in the UI.
    return max(1, depth_from(root_course, set()))


def collect_graph_requirements(
    root_course,
    current,
    max_depth=3
):
    """
    Load every course reachable from the selected course up to max_depth.

    Depth 1 shows only immediate prerequisites/corequisites.
    Depth 2 includes one more generation, and so on.
    """
    requirements = {}
    coreq_edges = set()

    # Store the shallowest depth where a course was encountered.
    best_depth_seen = {}
    visiting = set()

    def visit(course_code, depth=0):
        course_code = normalize_course_code(course_code)

        if not course_code:
            return

        if course_code not in current:
            current.append(course_code)

        if course_code in visiting:
            return

        previous_depth = best_depth_seen.get(course_code)

        if previous_depth is not None and previous_depth <= depth:
            return

        best_depth_seen[course_code] = depth

        if not load_course(course_code):
            return

        visiting.add(course_code)

        data = course_data[course_code]

        groups = data.get(
            "prereq_groups",
            [[course] for course in data.get("prereqs", [])]
        )

        cleaned_groups = []

        # Only expand dependencies while still inside the chosen depth.
        if depth < max_depth:
            for group in groups:
                normalized_group = tuple(
                    course
                    for course in normalize_prereq_group(group)
                    if course != course_code
                )

                if not normalized_group:
                    continue

                if normalized_group not in cleaned_groups:
                    cleaned_groups.append(normalized_group)

                for prereq in normalized_group:
                    visit(prereq, depth + 1)

            for coreq in data.get("coreqs", []):
                coreq = normalize_course_code(coreq)

                if not coreq or coreq == course_code:
                    continue

                coreq_edges.add((coreq, course_code))
                visit(coreq, depth + 1)

        requirements[course_code] = cleaned_groups

        visiting.discard(course_code)

    visit(root_course, 0)

    return requirements, coreq_edges


def or_node_id(group):
    """
    Produce a stable Graphviz node ID for one logical OR expression.
    """
    return "OR__" + "__".join(
        safe_name(course)
        for course in group
    )


def find_reusable_or_inputs(group, all_or_groups):
    """
    Find smaller existing OR groups that can feed into a larger OR group.

    Example:
        existing: (A OR B)
        target:   (A OR B OR C)

    returns:
        subgroup inputs: [(A, B)]
        direct inputs:   [C]

    If two disjoint reusable groups exist, both can feed the larger gate.
    """
    group_set = set(group)

    proper_subsets = [
        candidate
        for candidate in all_or_groups
        if (
            len(candidate) > 1
            and len(candidate) < len(group)
            and set(candidate).issubset(group_set)
        )
    ]

    # Prefer the largest reusable expressions first.
    proper_subsets.sort(
        key=lambda candidate: (
            -len(candidate),
            candidate
        )
    )

    selected_subsets = []
    covered_courses = set()

    for candidate in proper_subsets:
        candidate_set = set(candidate)

        # Skip a candidate that adds no new coverage.
        if candidate_set.issubset(covered_courses):
            continue

        selected_subsets.append(candidate)
        covered_courses.update(candidate_set)

    direct_courses = [
        course
        for course in group
        if course not in covered_courses
    ]

    return selected_subsets, direct_courses


def getNodes(dot, current, course_wanted, graph_depth):
    """
    Build a top-to-bottom prerequisite hierarchy with shared OR gates.

    Identical OR expressions are represented once and can branch to
    multiple dependent courses. Larger OR expressions reuse smaller
    existing OR expressions when possible.
    """
    dot.attr(
        rankdir="TB",
        ranksep="0.75",
        nodesep="0.35",
        newrank="true"
    )

    requirements, coreq_edges = collect_graph_requirements(
        course_wanted,
        current,
        max_depth=graph_depth
    )

    # Collect each unique alternative group once.
    all_or_groups = sorted(
        {
            group
            for groups in requirements.values()
            for group in groups
            if len(group) > 1
        },
        key=lambda group: (
            len(group),
            group
        )
    )

    with dot.subgraph(name="PREREQUISITES") as prereq_graph:

        # Draw all normal course nodes first.
        for course_code in current:
            prereq_graph.node(
                course_code,
                course_code
            )

        # ---------------------------------------------------------
        # Build shared OR expressions from smallest to largest.
        # ---------------------------------------------------------
        for group in all_or_groups:
            node_id = or_node_id(group)

            prereq_graph.node(
                node_id,
                "OR",
                shape="diamond",
                style="dashed",
                width="0.55",
                height="0.40",
                fontsize="10"
            )

            reusable_groups, direct_courses = (
                find_reusable_or_inputs(
                    group,
                    all_or_groups
                )
            )

            # Reuse smaller OR expressions as inputs.
            for subgroup in reusable_groups:
                prereq_graph.edge(
                    or_node_id(subgroup),
                    node_id
                )

            # Any course not already represented by a reused subgroup
            # feeds directly into this OR node.
            for prereq in direct_courses:
                prereq_graph.edge(
                    prereq,
                    node_id
                )

        # ---------------------------------------------------------
        # Connect prerequisite expressions to dependent courses.
        # ---------------------------------------------------------
        drawn_requirement_edges = set()

        for target_course, groups in requirements.items():
            for group in groups:

                if len(group) == 1:
                    source = group[0]
                    edge_key = (
                        source,
                        target_course,
                        "direct"
                    )

                    if edge_key not in drawn_requirement_edges:
                        prereq_graph.edge(
                            source,
                            target_course
                        )
                        drawn_requirement_edges.add(
                            edge_key
                        )

                else:
                    source = or_node_id(group)
                    edge_key = (
                        source,
                        target_course,
                        "or"
                    )

                    # One shared OR node can fan out to many courses.
                    if edge_key not in drawn_requirement_edges:
                        prereq_graph.edge(
                            source,
                            target_course
                        )
                        drawn_requirement_edges.add(
                            edge_key
                        )

    # Corequisites remain visible and are placed on the same horizontal
    # rank as the course they belong to. This makes corequisite
    # relationships visually distinct from prerequisite chains.
    with dot.subgraph(name="COREQUISITES") as coreq_graph:
        for pair_index, (coreq, target_course) in enumerate(
            sorted(coreq_edges)
        ):
            # A dedicated rank=same subgraph keeps this pair at the
            # same vertical level.
            with coreq_graph.subgraph(
                name=f"COREQ_RANK_{pair_index}"
            ) as same_rank:
                same_rank.attr(rank="same")

                same_rank.node(
                    coreq,
                    coreq
                )

                same_rank.node(
                    target_course,
                    target_course
                )

            # Keep the double-headed corequisite edge from affecting
            # the prerequisite hierarchy.
            coreq_graph.edge(
                coreq,
                target_course,
                constraint="false"
            )

    # Keep the selected course at the lowest rank.
    with dot.subgraph(name="SELECTED_COURSE_RANK") as selected_rank:
        selected_rank.attr(rank="sink")

        selected_rank.node(
            course_wanted,
            course_wanted,
            style="bold",
            penwidth="2"
        )


def graph_context():
    """
    Return the current user's course, depth, visible graph-course list,
    and deterministic session-specific graph filename.
    """
    major_name = current_major()
    course_wanted = current_course()
    graph_depth = current_graph_depth()
    courses = get_current_courses()

    if (
        not major_name
        or not course_wanted
        or course_wanted not in courses
    ):
        return None

    currents = []

    # Collect the dropdown options without regenerating the image.
    collect_graph_requirements(
        course_wanted,
        currents,
        max_depth=graph_depth
    )

    file_stem = (
        f"{get_session_id()}_"
        f"{safe_name(course_wanted)}_"
        f"UF_{safe_name(major_name)}_"
        f"D{graph_depth}"
    )

    return {
        "major": major_name,
        "course_wanted": course_wanted,
        "graph_depth": graph_depth,
        "currents": currents,
        "coursefile_stem": file_stem,
        "coursefile": file_stem + ".jpg",
    }


def render_current_graph():
    """
    Generate the current user's graph. Returns a context dictionary,
    or None if no valid course is selected.
    """
    context = graph_context()

    if context is None:
        return None

    course_wanted = context["course_wanted"]
    graph_depth = context["graph_depth"]

    load_course(course_wanted)

    dot = Digraph(
        f"UF {context['major']} Catalog",
        node_attr={"shape": "box"}
    )

    currents = []

    getNodes(
        dot,
        currents,
        course_wanted,
        graph_depth
    )

    dot.format = "jpg"

    dot.render(
        os.path.join(
            "static",
            context["coursefile_stem"]
        ),
        view=False,
        cleanup=True
    )

    # Use the list generated during graph creation.
    context["currents"] = currents

    save_course_index()

    return context


@app.route("/course", methods=["POST"])
def course():
    selected_course = normalize_course_code(
        request.form.get(
            "course",
            ""
        )
    )

    if selected_course == "":
        return redirect("/")

    courses = get_current_courses()

    if selected_course not in courses:
        return redirect("/")

    session["course_wanted"] = selected_course
    session["selected_current"] = ""

    max_depth = calculate_max_graph_depth(
        selected_course
    )

    session["max_graph_depth"] = max_depth
    session["graph_depth"] = min(
        3,
        get_selectable_depth_max(max_depth)
    )

    context = render_current_graph()

    if context is None:
        return redirect("/")

    return render_template(
        "index.html",
        **template_data(
            currents=context["currents"],
            coursefile=context["coursefile"],
        ),
    )


@app.route("/depth", methods=["POST"])
def change_depth():
    if not current_course():
        return redirect("/")

    requested_depth = request.form.get(
        "graph_depth",
        ""
    ).strip()

    selectable_max = get_selectable_depth_max()

    try:
        graph_depth = max(
            1,
            min(
                selectable_max,
                int(requested_depth)
            )
        )
    except ValueError:
        graph_depth = min(
            3,
            selectable_max
        )

    session["graph_depth"] = graph_depth
    session["selected_current"] = ""

    context = render_current_graph()

    if context is None:
        return redirect("/")

    return render_template(
        "index.html",
        **template_data(
            currents=context["currents"],
            coursefile=context["coursefile"],
        ),
    )


@app.route("/course/hide", methods=["POST"])
def hide_course_details():
    session["selected_current"] = ""

    context = graph_context()

    if context is None:
        return redirect("/")

    return render_template(
        "index.html",
        **template_data(
            currents=context["currents"],
            coursefile=context["coursefile"],
        ),
    )


@app.route("/course/show", methods=["POST"])
def show_course():
    selected_current = normalize_course_code(
        request.form.get(
            "currents",
            ""
        )
    )

    context = graph_context()

    if context is None:
        return redirect("/")

    if selected_current == "":
        session["selected_current"] = ""

        return render_template(
            "index.html",
            **template_data(
                currents=context["currents"],
                coursefile=context["coursefile"],
            ),
        )

    load_course(selected_current)
    session["selected_current"] = selected_current

    return render_template(
        "index.html",
        **template_data(
            currents=context["currents"],
            coursefile=context["coursefile"],
            showCurrents=toPrint(selected_current),
            selected_current=selected_current,
        ),
    )


@app.route("/health")
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )