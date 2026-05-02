PDF = report.pdf
SRC = report.tex

all: $(PDF)

$(PDF): $(SRC)
	pdflatex -interaction=nonstopmode $(SRC) >/dev/null
	pdflatex -interaction=nonstopmode $(SRC) >/dev/null

clean:
	rm -f *.aux *.log *.out *.toc *.bbl *.blg

distclean: clean
	rm -f $(PDF)

.PHONY: all clean distclean
