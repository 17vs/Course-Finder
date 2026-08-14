import json
import os
import re
import time

import requests
from bs4 import BeautifulSoup


CATALOG_DIR = "catalog"
MAJORS_FILE = "majors.json"
OUTPUT_FILE = "course_index.json"

os.makedirs(CATALOG_DIR, exist_ok=True)


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def catalog_path(subject_name):
    return os.path.join(
        CATALOG_DIR,
        f"UF_{safe_name(subject_name)}_Catalog.html"
    )


def get_catalog_html(subject_name, url):
    """
    Reuse a locally cached catalog if it already exists.
    Otherwise download it and save it in catalog/.
    """
    path = catalog_path(subject_name)

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as file:
            return file.read()

    print(f"Downloading: {subject_name}")

    response = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "Course-Finder/1.0"}
    )
    response.raise_for_status()

    with open(path, "w", encoding="utf-8") as file:
        file.write(response.text)

    # Be polite to the catalog server when many pages need downloading.
    time.sleep(0.15)

    return response.text


def extract_course_codes(html):
    soup = BeautifulSoup(html, "html.parser")
    codes = []

    for block in soup.find_all(
        "div",
        class_="courseblock courseblocktoggle"
    ):
        title = block.find(class_="courseblocktitle")

        if not title:
            continue

        match = re.search(
            r"\b([A-Z]{2,4})\s+(\d{4}[A-Z]?)\b",
            title.get_text(" ", strip=True)
        )

        if match:
            codes.append(f"{match.group(1)} {match.group(2)}")

    return codes


def main():
    with open(MAJORS_FILE, "r", encoding="utf-8") as file:
        majors = json.load(file)

    course_index = {}
    duplicates = {}

    for number, (subject_name, url) in enumerate(majors.items(), start=1):
        print(f"[{number}/{len(majors)}] Indexing {subject_name}")

        try:
            html = get_catalog_html(subject_name, url)
            codes = extract_course_codes(html)
        except Exception as exc:
            print(f"  WARNING: could not index {subject_name}: {exc}")
            continue

        for code in codes:
            if code in course_index and course_index[code] != subject_name:
                duplicates.setdefault(code, [course_index[code]])

                if subject_name not in duplicates[code]:
                    duplicates[code].append(subject_name)

                # Keep the first catalog as the default location.
                continue

            course_index[code] = subject_name

        print(f"  Found {len(codes)} courses")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
        json.dump(
            course_index,
            file,
            indent=2,
            ensure_ascii=False,
            sort_keys=True
        )

    print()
    print(f"Created {OUTPUT_FILE} with {len(course_index)} courses.")

    if duplicates:
        print(
            f"Note: {len(duplicates)} course codes appeared in more than one "
            "catalog. The first catalog found is used."
        )


if __name__ == "__main__":
    main()
