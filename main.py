import requests
from bs4 import BeautifulSoup
from graphviz import Digraph
import re
from flask import Flask, request, render_template, redirect
import validators

url = "https://catalog.ufl.edu/UGRD/courses/electrical_and_computer_engineering/"
major = "EE"
app = Flask(__name__, static_folder='static')


def runURL(url, major):
    courses.clear()
    descriptions.clear()
    prerecs.clear()
    corecs.clear()
    response = requests.get(url)
    with open(f"UF_{major}_Catalog.html", "w") as file:
        file.write(response.text)
        file.close()
    getLists(courses, descriptions, prerecs, corecs, major)


def find_course_names(text):
    pattern = r"[A-Z]{2,4}\s\d{4}[A-Z]?"
    course_names = re.findall(pattern, text)
    return course_names


def getLists(courses, descriptions, prerecs, corecs, major):
    num = 0
    with open(f"UF_{major}_Catalog.html") as file:
        soup = BeautifulSoup(file, 'html.parser')
        num_courses = soup.find_all('div', class_='courseblock courseblocktoggle')
        for i in range(len(num_courses)):
            courses.append("")
            descriptions.append("")
            prerecs[i] = []
            corecs[i] = []
        for course in num_courses:
            info = ""
            titindex = course.find(class_="courseblocktitle")
            cr = titindex.find('span', class_='credits')
            t = titindex.text.strip().split()
            title = " ".join(t[:2])
            # if there is a credit, set value to credit
            # title becomes the course name without credit (last 2)
            # removing first two elements as those are the course name
            if cr:
                credit = " | " + cr.text
                nameTitle = " - " + " ".join(t[2:-2])
            # no credit, title is enire course name until end
            else:
                credit = ""
                nameTitle = " - " + " ".join(t[2:])
            description = course.find(class_='courseblockdesc')
            info += nameTitle + credit + "<br>" + description.text.strip() + "<br>"
            courses[num] = title
            for extr in course.find_all(class_='courseblockextra'):
                word = ''.join([l for l in extr.text.strip().split()[0] if l.isalpha()]).lower()
                if word in "gradingscheme":
                    info += " ".join(extr.text.strip().split()[0:]) + "<br>"
                    descriptions[num] = info
                elif word in "prerequisite":
                    prerec = find_course_names(extr.text.strip())
                    if not prerec:
                        break
                    for pre in prerec:
                        info += pre + "<br>"
                        prerecs[num].append(pre.replace(u'\xa0', u' '))
                elif word in "corequisite":
                    corec = find_course_names(extr.text.strip())
                    if not corec:
                        break
                    for co in corec:
                        info += co + "<br>"
            num += 1
    file.close()


def toPrint(c):
    n = courses.index(c) if c in courses else -1
    if n < 0:
        return c
    text = courses[n] + descriptions[n]
    if prerecs[n]:
        text += "Prerequisites: " + "<br>" + ', '.join([i for i in prerecs[n]]) + "<br>"
    if corecs[n]:
        text += "Corequisites: " + "<br>" + ', '.join([i for i in corecs[n]]) + "<br>"
    return text


courses = []
descriptions = []
prerecs = {}
corecs = {}
runURL(url, major)


@app.route('/')
@app.route('/home')
def home():
    return render_template("index.html", courses=courses, major=major)


def drawRecs(sub1, sub2, courseitem, current, reclist, colist, drawn_edges=None):
    if courseitem not in current:
        current.append(courseitem)
    if drawn_edges is None:
        drawn_edges = set()
    rind = courses.index(courseitem) if courseitem in courses else -1
    if rind >= 0:
        for c in colist[rind]:
            if c != courseitem:
                sub1.node(str(c), str(c))
                edge = (str(c), str(courseitem))
                if edge not in drawn_edges:
                    sub1.edge(*edge, dir='both', minlen='3.0')
                    drawn_edges.add(edge)
            drawRecs(sub1, sub2, c, current, reclist, colist, drawn_edges)
        for r in reclist[rind]:
            if r != courseitem:
                sub2.node(str(r), str(r))
                edge = (str(r), str(courseitem))
                if edge not in drawn_edges:
                    sub2.edge(*edge)
                    drawn_edges.add(edge)
            drawRecs(sub1, sub2, r, current, reclist, colist, drawn_edges)


def getNodes(dot, current):
    with dot.subgraph(name='TOP') as subT, dot.subgraph(name='PRE') as subP:
        subT.attr(rank='same')
        subT.node(str(course_wanted), str(course_wanted), style='bold')
        drawRecs(subT, subP, course_wanted, current, prerecs, corecs)


@app.route('/course', methods=['POST'])
def course():
    global course_wanted
    course_wanted = request.form['course'].upper()
    if course_wanted not in courses:
        return redirect('/')
    dot = Digraph(f'UF {major} Catalog', node_attr={'shape': 'box'})
    global currents
    currents = []
    getNodes(dot, currents)
    dot.format = 'jpg'
    global coursefile
    coursefile = f'{course_wanted}_UF_{major}'
    dot.render(f"static/{coursefile}", view=False, cleanup=True)
    return render_template("index.html", courses=courses, course_wanted=course_wanted, currents=currents, coursefile=coursefile + ".jpg", major=major)


@app.route('/course/show', methods=['POST'])
def show_course():
    cur = request.form['currents']
    return render_template("index.html", courses=courses, course_wanted=course_wanted, currents=currents, coursefile=coursefile + ".jpg", showCurrents=toPrint(cur), major=major)


@app.route('/geturl', methods=['GET', 'POST'])
def get_url():
    seturl = request.form.get('inputurl')
    global major
    major = request.form.get('inputmajor')
    if major and validators.url(seturl):
        runURL(seturl, major)
        return redirect('/')
    else:
        return render_template("index.html")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)
