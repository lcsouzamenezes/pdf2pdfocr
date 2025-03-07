#!/usr/bin/env python3
##############################################################################
# Copyright (c) 2023: Leonardo Cardoso
# https://github.com/LeoFCardoso/pdf2pdfocr
##############################################################################
# OCR a PDF and add a text "layer" in the original file (a so called "pdf sandwich")
# Use only open source tools.
# Unless requested, does not re-encode the images inside an unprotected PDF file.
# Leonardo Cardoso - inspired in ocrmypdf (https://github.com/jbarlow83/OCRmyPDF)
# and this post: https://github.com/jbarlow83/OCRmyPDF/issues/8
###############################################################################
import argparse
import configparser
import datetime
import errno
import glob
import io
import itertools
import math
import multiprocessing
import os
import random
import re
import shlex
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from concurrent import futures
from pathlib import Path
from xml.etree import ElementTree

import PyPDF2
import psutil
from PIL import Image, ImageChops
from PyPDF2.errors import PdfReadError
from PyPDF2.generic import ByteStringObject
from bs4 import BeautifulSoup
from packaging.version import parse as parse_version
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas

__author__ = 'Leonardo F. Cardoso'

VERSION = '1.12.2 marapurense '


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, flush=True, **kwargs)


def do_pdftoimage(param_path_pdftoppm, param_page_range, param_input_file, param_image_resolution, param_tmp_dir,
                  param_prefix, param_shell_mode):
    """
    Will be called from multiprocessing, so no global variables are allowed.
    Convert PDF to image file.
    """
    command_line_list = [param_path_pdftoppm]
    first_page = 0
    last_page = 0
    if param_page_range is not None:
        first_page = param_page_range[0]
        last_page = param_page_range[1]
        command_line_list += ['-f', str(first_page), '-l', str(last_page)]
    #
    command_line_list += ['-r', str(param_image_resolution), '-jpeg', param_input_file, param_tmp_dir + param_prefix]
    pimage = subprocess.Popen(command_line_list, stdout=subprocess.DEVNULL,
                              stderr=open(param_tmp_dir + "pdftoppm_err_{0}-{1}-{2}.log".format(param_prefix, first_page, last_page), "wb"),
                              shell=param_shell_mode)
    pimage.wait()
    return pimage.returncode


def do_autorotate_info(param_image_file, param_shell_mode, param_temp_dir, param_tess_lang, param_path_tesseract, param_tesseract_version):
    """
    Will be called from multiprocessing, so no global variables are allowed.
    Do autorotate of images based on tesseract (execution with 'psm 0') information.
    """
    param_image_no_ext = os.path.splitext(os.path.basename(param_image_file))[0]
    psm_parameter = "-psm" if (param_tesseract_version == 3) else "--psm"
    tess_command_line = [param_path_tesseract, '-l', "osd+" + param_tess_lang, psm_parameter, '0', param_image_file,
                         param_temp_dir + param_image_no_ext]
    ptess1 = subprocess.Popen(tess_command_line,
                              stdout=open(param_temp_dir + "autorot_tess_out_{0}.log".format(param_image_no_ext), "wb"),
                              stderr=open(param_temp_dir + "autorot_tess_err_{0}.log".format(param_image_no_ext), "wb"),
                              shell=param_shell_mode)
    ptess1.wait()


def do_deskew(param_image_file, param_threshold, param_shell_mode, param_path_mogrify):
    """
    Will be called from multiprocessing, so no global variables are allowed.
    Do a deskew of image.
    """
    pd = subprocess.Popen([param_path_mogrify, '-deskew', param_threshold, param_image_file], shell=param_shell_mode)
    pd.wait()
    return True


def do_ocr_tesseract(param_image_file, param_extra_ocr_flag, param_tess_lang, param_tess_psm, param_temp_dir, param_shell_mode, param_path_tesseract,
                     param_text_generation_strategy, param_delete_temps, param_tess_can_textonly_pdf):
    """
    Will be called from multiprocessing, so no global variables are allowed.
    Do OCR of image with tesseract
    """
    param_image_no_ext = os.path.splitext(os.path.basename(param_image_file))[0]
    tess_command_line = [param_path_tesseract]
    if type(param_extra_ocr_flag) == str:
        tess_command_line.extend(param_extra_ocr_flag.split(" "))
    tess_command_line.extend(['-l', param_tess_lang])
    if param_text_generation_strategy == "tesseract":
        tess_command_line += ['-c', 'tessedit_create_pdf=1']
        if param_tess_can_textonly_pdf:
            tess_command_line += ['-c', 'textonly_pdf=1']
    #
    if param_text_generation_strategy == "native":
        tess_command_line += ['-c', 'tessedit_create_hocr=1']
    #
    tess_command_line += [
        '-c', 'tessedit_create_txt=1',
        '-c', 'tessedit_pageseg_mode=' + param_tess_psm,
        param_image_file, param_temp_dir + param_image_no_ext]
    pocr = subprocess.Popen(tess_command_line,
                            stdout=subprocess.DEVNULL,
                            stderr=open(param_temp_dir + "tess_err_{0}.log".format(param_image_no_ext), "wb"),
                            shell=param_shell_mode)
    pocr.wait()
    if param_text_generation_strategy == "tesseract" and (not param_tess_can_textonly_pdf):
        pdf_file = param_temp_dir + param_image_no_ext + ".pdf"
        pdf_file_tmp = param_temp_dir + param_image_no_ext + ".tesspdf"
        os.rename(pdf_file, pdf_file_tmp)
        output_pdf = PyPDF2.PdfWriter()
        desc_pdf_file_tmp = open(pdf_file_tmp, 'rb')
        tess_pdf = PyPDF2.PdfReader(desc_pdf_file_tmp, strict=False)
        for i in range(len(tess_pdf.pages)):
            imagepage = tess_pdf.pages[i]
            output_pdf.addPage(imagepage)
        #
        output_pdf.removeImages(ignoreByteStringObject=False)
        out_page = output_pdf.getPage(0)  # Tesseract PDF is always one page in this software
        # Hack to obtain smaller file (delete the image reference)
        out_page["/Resources"][PyPDF2.generic.createStringObject("/XObject")] = PyPDF2.generic.ArrayObject()
        out_page.compressContentStreams()
        with open(pdf_file, 'wb') as f:
            output_pdf.write(f)
        desc_pdf_file_tmp.close()
        # Try to save some temp space as tesseract generate PDF with same size of image
        if param_delete_temps:
            os.remove(pdf_file_tmp)
    #
    if param_text_generation_strategy == "native":
        hocr = HocrTransform(param_temp_dir + param_image_no_ext + ".hocr", 300)
        hocr.to_pdf(param_temp_dir + param_image_no_ext + ".pdf", image_file_name=None, show_bounding_boxes=False,
                    invisible_text=True)
    # Track progress in all situations
    Path(param_temp_dir + param_image_no_ext + ".tmp").touch()  # .tmp files are used to track overall progress


def do_ocr_cuneiform(param_image_file, param_extra_ocr_flag, param_cunei_lang, param_temp_dir, param_shell_mode, param_path_cunei):
    """
    Will be called from multiprocessing, so no global variables are allowed.
    Do OCR of image with cuneiform
    """
    param_image_no_ext = os.path.splitext(os.path.basename(param_image_file))[0]
    cunei_command_line = [param_path_cunei]
    if type(param_extra_ocr_flag) == str:
        cunei_command_line.extend(param_extra_ocr_flag.split(" "))
    cunei_command_line.extend(['-l', param_cunei_lang.lower(), "-f", "hocr", "-o", param_temp_dir + param_image_no_ext + ".hocr", param_image_file])
    #
    pocr = subprocess.Popen(cunei_command_line,
                            stdout=open(param_temp_dir + "cuneif_out_{0}.log".format(param_image_no_ext), "wb"),
                            stderr=open(param_temp_dir + "cuneif_err_{0}.log".format(param_image_no_ext), "wb"),
                            shell=param_shell_mode)
    pocr.wait()
    # Sometimes, cuneiform fails to OCR and expected HOCR file is missing. Experiments show that English can be used to try a workaround.
    if not os.path.isfile(param_temp_dir + param_image_no_ext + ".hocr") and param_cunei_lang.lower() != "eng":
        eprint("Warning: fail to OCR file '{0}'. Trying again with English language.".format(param_image_no_ext))
        cunei_command_line = [param_path_cunei]
        if type(param_extra_ocr_flag) == str:
            cunei_command_line.extend(param_extra_ocr_flag.split(" "))
        cunei_command_line.extend(['-l', "eng", "-f", "hocr", "-o", param_temp_dir + param_image_no_ext + ".hocr", param_image_file])
        pocr = subprocess.Popen(cunei_command_line,
                                stdout=open(param_temp_dir + "cuneif_out_eng_{0}.log".format(param_image_no_ext), "wb"),
                                stderr=open(param_temp_dir + "cuneif_err_eng_{0}.log".format(param_image_no_ext), "wb"),
                                shell=param_shell_mode)
        pocr.wait()
    #
    bs_parser = "lxml"
    if os.path.isfile(param_temp_dir + param_image_no_ext + ".hocr"):
        # Try to fix unclosed meta tags, as cuneiform HOCR may be not well-formed
        with open(param_temp_dir + param_image_no_ext + ".hocr", "r") as fpr:
            corrected_hocr = str(BeautifulSoup(fpr, bs_parser))
    else:
        eprint("Warning: fail to OCR file '{0}'. Page will not contain text.".format(param_image_no_ext))
        # TODO try to use the same size as original PDF page (bbox is hard coded by now to look like A4 page - portrait)
        corrected_hocr = str(BeautifulSoup('<div class="ocr_page" id="page_1" title="image x; bbox 0 0 1700 2400">', bs_parser))
    with open(param_temp_dir + param_image_no_ext + ".fixed.hocr", "w") as fpw:
        fpw.write(corrected_hocr)
    #
    hocr = HocrTransform(param_temp_dir + param_image_no_ext + ".fixed.hocr", 300)
    hocr.to_pdf(param_temp_dir + param_image_no_ext + ".pdf", image_file_name=None, show_bounding_boxes=False, invisible_text=True)
    # Track progress
    Path(param_temp_dir + param_image_no_ext + ".tmp").touch()  # .tmp files are used to track overall progress


def do_rebuild(param_image_file, param_path_convert, param_convert_params, param_tmp_dir, param_shell_mode):
    """
    Will be called from multiprocessing, so no global variables are allowed.
    Create one PDF file from image file.
    """
    param_image_no_ext = os.path.splitext(os.path.basename(param_image_file))[0]
    # http://stackoverflow.com/questions/79968/split-a-string-by-spaces-preserving-quoted-substrings-in-python
    convert_params_list = shlex.split(param_convert_params)
    command_rebuild = [param_path_convert, param_image_file] + convert_params_list + [param_tmp_dir + "REBUILD_" + param_image_no_ext + ".pdf"]
    prebuild = subprocess.Popen(
        command_rebuild,
        stdout=open(param_tmp_dir + "convert_log_{0}.log".format(param_image_no_ext), "wb"),
        stderr=open(param_tmp_dir + "convert_err_{0}.log".format(param_image_no_ext), "wb"),
        shell=param_shell_mode)
    prebuild.wait()


def do_check_img_greyscale(param_image_file):
    """
    Inspired in code provided by karl-k:
    https://stackoverflow.com/questions/23660929/how-to-check-whether-a-jpeg-image-is-color-or-gray-scale-using-only-python-stdli
    Check if image is monochrome (1 channel or 3 identical channels)
    """
    im = Image.open(param_image_file).convert('RGB')
    rgb = im.split()
    if ImageChops.difference(rgb[0], rgb[1]).getextrema()[1] != 0:
        return False
    if ImageChops.difference(rgb[0], rgb[2]).getextrema()[1] != 0:
        return False
    #
    return True


def do_check_img_colors_size(param_image_file):
    """
    Inspired in code provided by Raffael:
    https://stackoverflow.com/questions/14041562/python-pil-detect-if-an-image-is-completely-black-or-white
    Check if image is single color
    """
    im = Image.open(param_image_file)
    colors = im.getcolors()
    width, height = im.size
    return colors, (width, height)


def do_create_blank_pdf(param_filename_pdf, param_dimensions, param_image_resolution):
    blank_output_pdf = PyPDF2.PdfWriter()
    img_witdh = param_dimensions[0]
    img_width_pt = (img_witdh / param_image_resolution) * 72.0
    img_height = param_dimensions[1]
    img_height_pt = (img_height / param_image_resolution) * 72.0
    blank_output_pdf.addBlankPage(img_width_pt, img_height_pt)
    with open(param_filename_pdf, 'wb') as f:
        blank_output_pdf.write(f)
        f.close()


def percentual_float(x):
    x = float(x)
    if x <= 0.0 or x > 1.0:
        raise argparse.ArgumentTypeError("%r not in range (0.0, 1.0]" % (x,))
    return x


class HocrTransformError(Exception):
    pass


class HocrTransform:
    """
    A class for converting documents from the hOCR format.
    For details of the hOCR format, see:
    http://docs.google.com/View?docid=dfxcv4vc_67g844kf

    Adapted from https://github.com/jbarlow83/OCRmyPDF/blob/master/ocrmypdf/hocrtransform.py
    """

    def __init__(self, hocr_file_name, dpi):
        self.rect = namedtuple('Rect', ['x1', 'y1', 'x2', 'y2'])
        self.dpi = dpi
        self.boxPattern = re.compile(r'bbox((\s+\d+){4})')
        self.hocr = ElementTree.parse(hocr_file_name)
        # if the hOCR file has a namespace, ElementTree requires its use to
        # find elements
        matches = re.match(r'({.*})html', self.hocr.getroot().tag)
        self.xmlns = ''
        if matches:
            self.xmlns = matches.group(1)
        # get dimension in pt (not pixel!!!!) of the OCRed image
        self.width, self.height = None, None
        for div in self.hocr.findall(
                ".//%sdiv[@class='ocr_page']" % self.xmlns):
            coords = self.element_coordinates(div)
            pt_coords = self.pt_from_pixel(coords)
            self.width = pt_coords.x2 - pt_coords.x1
            self.height = pt_coords.y2 - pt_coords.y1
            # there shouldn't be more than one, and if there is, we don't want it
            break
        if self.width is None or self.height is None:
            raise HocrTransformError("hocr file is missing page dimensions")

    def __str__(self):
        """
        Return the textual content of the HTML body
        """
        if self.hocr is None:
            return ''
        body = self.hocr.find(".//%sbody" % self.xmlns)
        if body:
            return self._get_element_text(body)
        else:
            return ''

    def _get_element_text(self, element):
        """
        Return the textual content of the element and its children
        """
        text = ''
        if element.text is not None:
            text += element.text
        for child in element:
            text += self._get_element_text(child)
        if element.tail is not None:
            text += element.tail
        return text

    def element_coordinates(self, element):
        """
        Returns a tuple containing the coordinates of the bounding box around
        an element
        """
        out = (0, 0, 0, 0)
        if 'title' in element.attrib:
            matches = self.boxPattern.search(element.attrib['title'])
            if matches:
                coords = matches.group(1).split()
                out = self.rect._make(int(coords[n]) for n in range(4))
        return out

    def pt_from_pixel(self, pxl):
        """
        Returns the quantity in PDF units (pt) given quantity in pixels
        """
        return self.rect._make(
            (c / self.dpi * inch) for c in pxl)

    def replace_unsupported_chars(self, s):
        """
        Given an input string, returns the corresponding string that:
        - is available in the helvetica facetype
        - does not contain any ligature (to allow easy search in the PDF file)
        """
        # The 'u' before the character to replace indicates that it is a
        # unicode character
        s = s.replace(u"ﬂ", "fl")
        s = s.replace(u"ﬁ", "fi")
        return s

    def to_pdf(self, out_file_name, image_file_name=None, show_bounding_boxes=False, fontname="Helvetica",
               invisible_text=True):
        """
        Creates a PDF file with an image superimposed on top of the text.
        Text is positioned according to the bounding box of the lines in
        the hOCR file.
        The image need not be identical to the image used to create the hOCR
        file.
        It can have a lower resolution, different color mode, etc.
        """
        # create the PDF file
        # page size in points (1/72 in.)
        pdf = Canvas(
            out_file_name, pagesize=(self.width, self.height), pageCompression=1)
        # draw bounding box for each paragraph
        # light blue for bounding box of paragraph
        pdf.setStrokeColorRGB(0, 1, 1)
        # light blue for bounding box of paragraph
        pdf.setFillColorRGB(0, 1, 1)
        pdf.setLineWidth(0)  # no line for bounding box
        for elem in self.hocr.findall(
                ".//%sp[@class='%s']" % (self.xmlns, "ocr_par")):
            elemtxt = self._get_element_text(elem).rstrip()
            if len(elemtxt) == 0:
                continue
            pxl_coords = self.element_coordinates(elem)
            pt = self.pt_from_pixel(pxl_coords)
            # draw the bbox border
            if show_bounding_boxes:
                pdf.rect(pt.x1, self.height - pt.y2, pt.x2 - pt.x1, pt.y2 - pt.y1, fill=1)
        # check if element with class 'ocrx_word' are available
        # otherwise use 'ocr_line' as fallback
        elemclass = "ocr_line"
        if self.hocr.find(".//%sspan[@class='ocrx_word']" % self.xmlns) is not None:
            elemclass = "ocrx_word"
        # itterate all text elements
        # light green for bounding box of word/line
        pdf.setStrokeColorRGB(1, 0, 0)
        pdf.setLineWidth(0.5)  # bounding box line width
        pdf.setDash(6, 3)  # bounding box is dashed
        pdf.setFillColorRGB(0, 0, 0)  # text in black
        for elem in self.hocr.findall(".//%sspan[@class='%s']" % (self.xmlns, elemclass)):
            elemtxt = self._get_element_text(elem).rstrip()
            elemtxt = self.replace_unsupported_chars(elemtxt)
            if len(elemtxt) == 0:
                continue
            pxl_coords = self.element_coordinates(elem)
            pt = self.pt_from_pixel(pxl_coords)
            # draw the bbox border
            if show_bounding_boxes:
                pdf.rect(pt.x1, self.height - pt.y2, pt.x2 - pt.x1, pt.y2 - pt.y1, fill=0)
            text = pdf.beginText()
            fontsize = pt.y2 - pt.y1
            text.setFont(fontname, fontsize)
            if invisible_text:
                text.setTextRenderMode(3)  # Invisible (indicates OCR text)
            # set cursor to bottom left corner of bbox (adjust for dpi)
            text.setTextOrigin(pt.x1, self.height - pt.y2)
            # scale the width of the text to fill the width of the bbox
            text.setHorizScale(100 * (pt.x2 - pt.x1) / pdf.stringWidth(elemtxt, fontname, fontsize))
            # write the text to the page
            text.textLine(elemtxt)
            pdf.drawText(text)
        #
        # put the image on the page, scaled to fill the page
        if image_file_name is not None:
            pdf.drawImage(image_file_name, 0, 0, width=self.width, height=self.height)
        # finish up the page and save it
        pdf.showPage()
        pdf.save()
        #


class Pdf2PdfOcrException(Exception):
    pass


class Pdf2PdfOcr:
    # External tools command. If you can't edit your path, adjust here to match your system
    cmd_cuneiform = "cuneiform"
    path_cuneiform = ""
    cmd_tesseract = "tesseract"
    path_tesseract = ""
    cmd_convert = "convert"
    cmd_magick = "magick"  # used on Windows with ImageMagick 7+ (to avoid conversion path problems)
    path_convert = ""
    cmd_mogrify = "mogrify"
    path_mogrify = ""
    cmd_file = "file"
    path_file = ""
    cmd_pdftoppm = "pdftoppm"
    path_pdftoppm = ""
    cmd_pdffonts = "pdffonts"
    path_pdffonts = ""
    cmd_ps2pdf = "ps2pdf"
    path_ps2pdf = ""
    cmd_pdf2ps = "pdf2ps"
    path_pdf2ps = ""
    cmd_gs = "gs"
    path_gs = ""
    cmd_qpdf = "qpdf"
    path_qpdf = ""

    tesseract_can_textonly_pdf = False
    """Since Tesseract 3.05.01, new use case of tesseract - https://github.com/tesseract-ocr/tesseract/issues/660"""

    tesseract_version = 3
    """Tesseract version installed on system"""

    extension_images = "jpg"
    """Temp images will use this extension. Using jpg to avoid big temp files in pdf with a lot of pages"""

    output_file = ""
    """The PDF output file"""

    output_file_text = ""
    """The TXT output file"""

    path_this_python = sys.executable
    """Path for python in this system"""

    shell_mode = (sys.platform == "win32")
    """How to run external process? In Windows use Shell=True
    http://stackoverflow.com/questions/5658622/python-subprocess-popen-environment-path
    "Also, on Windows with shell=False, it pays no attention to PATH at all,
    and will only look in relative to the current working directory."
    """

    def __init__(self, args, override_input_file=None):
        super().__init__()
        self.log_time_format = '%Y-%m-%d %H:%M:%S.%f'
        #
        # A random prefix to support multiple execution in parallel
        self.prefix = ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(5))
        # The temp dir
        self.tmp_dir = tempfile.gettempdir() + os.path.sep + "pdf2pdfocr_{0}".format(self.prefix) + os.path.sep
        os.mkdir(self.tmp_dir)
        #
        self.verbose_mode = args.verbose_mode
        self.check_external_tools()
        # Handle arguments from command line
        self.safe_mode = args.safe_mode
        self.check_text_mode = args.check_text_mode
        self.ignore_existing_text = args.ignore_existing_text
        self.blank_pages = []
        self.blank_pages_dimensions = []
        self.check_protection_mode = args.check_protection_mode
        self.avoid_high_pages_mode = args.max_pages is not None
        self.avoid_high_pages_pages = args.max_pages
        self.avoid_small_file_mode = args.min_kbytes is not None
        self.avoid_small_file_limit_kb = args.min_kbytes
        self.force_rebuild_mode = args.force_rebuild_mode
        self.rebuild_pdf_from_images = False
        self.user_convert_params = args.convert_params
        if self.user_convert_params is None:
            self.user_convert_params = ""  # Default
        self.deskew_threshold = args.deskew_percent
        self.use_deskew_mode = args.deskew_percent is not None
        self.use_autorotate = args.autorotate
        self.parallel_threshold = args.parallel_percent
        if self.parallel_threshold is None:
            self.parallel_threshold = 1  # Default
        self.create_text_mode = args.create_text_mode
        self.force_out_file_mode = args.output_file is not None
        if self.force_out_file_mode:
            self.force_out_file = args.output_file
        else:
            self.force_out_file = ""
        self.force_out_dir_mode = args.output_dir is not None
        if self.force_out_dir_mode:
            self.force_out_dir = args.output_dir
        else:
            self.force_out_dir = ""
        if self.force_out_file != "" and self.force_out_dir != "":
            raise Pdf2PdfOcrException("It's not possible to force output name and dir at the same time. Please use '-o' OR '-O'")
        if self.force_out_dir_mode and (not os.path.isdir(self.force_out_dir)):
            raise Pdf2PdfOcrException("Invalid output directory: {0}".format(self.force_out_dir))
        self.tess_langs = args.tess_langs
        if self.tess_langs is None:
            self.tess_langs = "por+eng"  # Default
        self.tess_psm = args.tess_psm
        if self.tess_psm is None:
            self.tess_psm = "1"  # Default
        self.image_resolution = args.image_resolution
        self.text_generation_strategy = args.text_generation_strategy
        if self.text_generation_strategy not in ["tesseract", "native"]:
            raise Pdf2PdfOcrException("{0} is not a valid text generation strategy. Exiting.".format(self.text_generation_strategy))
        self.ocr_ignored = False
        self.ocr_engine = args.ocr_engine
        if self.ocr_engine not in ["tesseract", "cuneiform", "no_ocr"]:
            raise Pdf2PdfOcrException("{0} is not a valid ocr engine. Exiting.".format(self.ocr_engine))
        self.extra_ocr_flag = args.extra_ocr_flag
        if self.extra_ocr_flag is not None:
            self.extra_ocr_flag = str(self.extra_ocr_flag.strip())
        self.delete_temps = not args.keep_temps
        self.input_file = args.input_file if override_input_file is None else override_input_file
        if not os.path.isfile(self.input_file):
            raise Pdf2PdfOcrException("{0} not found. Exiting.".format(self.input_file))
        self.input_file = os.path.abspath(self.input_file)
        self.input_file_type = ""
        #
        self.input_file_has_text = False
        self.input_file_is_encrypted = False
        self.input_file_metadata = dict()
        self.input_file_number_of_pages = None
        #
        self.debug("Temp dir is {0}".format(self.tmp_dir))
        self.debug("Prefix is {0}".format(self.prefix))
        # Where am I?
        self.script_dir = os.path.dirname(os.path.abspath(__file__)) + os.path.sep
        self.debug("Script dir is {0}".format(self.script_dir))
        #
        self.cpu_to_use = int(multiprocessing.cpu_count() * self.parallel_threshold)
        if self.cpu_to_use == 0:
            self.cpu_to_use = 1
        self.debug("Parallel operations will use {0} CPUs".format(self.cpu_to_use))
        #
        self.main_pool = multiprocessing.Pool(self.cpu_to_use)
        #

    def check_external_tools(self):
        """Check if external tools are available, aborting or warning in case of any error."""
        self.path_tesseract = shutil.which(self.cmd_tesseract)
        if self.path_tesseract is None:
            eprint("tesseract not found. Aborting...")
            sys.exit(1)
        #
        self.tesseract_can_textonly_pdf = self.test_tesseract_textonly_pdf()
        self.tesseract_version = self.get_tesseract_version()
        #
        self.path_cuneiform = shutil.which(self.cmd_cuneiform)
        if self.path_cuneiform is None:
            self.debug("cuneiform not available")
        #
        # Try to avoid errors on Windows with native OS "convert" command
        # http://savage.net.au/ImageMagick/html/install-convert.html
        # https://www.imagemagick.org/script/magick.php
        self.path_convert = shutil.which(self.cmd_convert)
        if not self.test_convert():
            self.path_convert = shutil.which(self.cmd_magick)
        if self.path_convert is None:
            eprint("convert/magick from ImageMagick not found. Aborting...")
            sys.exit(1)
        #
        self.path_mogrify = shutil.which(self.cmd_mogrify)
        if self.path_mogrify is None:
            eprint("mogrify from ImageMagick not found. Aborting...")
            sys.exit(1)
        #
        self.path_file = shutil.which(self.cmd_file)
        if self.path_file is None:
            eprint("file not found. Aborting...")
            sys.exit(1)
        #
        self.path_pdftoppm = shutil.which(self.cmd_pdftoppm)
        if self.path_pdftoppm is None:
            eprint("pdftoppm (poppler) not found. Aborting...")
            sys.exit(1)
        if self.get_pdftoppm_version() <= parse_version("0.70.0"):
            self.log("External tool 'pdftoppm' is outdated. Please upgrade poppler")
        #
        self.path_pdffonts = shutil.which(self.cmd_pdffonts)
        if self.path_pdffonts is None:
            eprint("pdffonts (poppler) not found. Aborting...")
            sys.exit(1)
        #
        self.path_ps2pdf = shutil.which(self.cmd_ps2pdf)
        self.path_pdf2ps = shutil.which(self.cmd_pdf2ps)
        if self.path_ps2pdf is None or self.path_pdf2ps is None:
            eprint("ps2pdf or pdf2ps (ghostscript) not found. File repair will not work...")
        #
        self.path_gs = shutil.which(self.cmd_gs)
        if self.path_gs is None:
            eprint("ghostscript not found. Param 'ignore-existing-text' will not work...")
        #
        self.path_qpdf = shutil.which(self.cmd_qpdf)
        if self.path_qpdf is None:
            self.log("External tool 'qpdf' not available. Merge can be slow")
        else:
            qpdf_version = self.get_qpdf_version()
            minimum_version = "8.4.1"
            if qpdf_version < parse_version(minimum_version):
                self.log("External tool 'qpdf' is not on minimum version ({0}). Merge can be slow".format(minimum_version))
                self.path_qpdf = None
        #

    def debug(self, param):
        try:
            if self.verbose_mode:
                tstamp = datetime.datetime.now().strftime(self.log_time_format)
                print("[{0}] [DEBUG] {1}".format(tstamp, param), flush=True)
        except:
            pass

    def log(self, param):
        try:
            tstamp = datetime.datetime.now().strftime(self.log_time_format)
            print("[{0}] [LOG] {1}".format(tstamp, param), flush=True)
        except:
            pass

    def cleanup(self):
        #
        # Try to kill all child process still alive (in timeout situation)
        process = psutil.Process(os.getpid())
        for proc in process.children(recursive=True):
            if "python" not in proc.name().lower():  # Python process are from multiprocessing and will be handled below
                self.debug("Killing child process {0} with pid {1}".format(proc.name(), proc.pid))
                try:
                    proc.kill()
                except:
                    pass  # By design
        #
        # Cleanup the pool
        if self.main_pool:
            self.main_pool.close()
            self.main_pool.terminate()
            self.main_pool.join()
            self.main_pool = None  # Signal for pool to stop waiting in while loops
        #
        # Cleanup temp files
        if self.delete_temps:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        else:
            eprint("Temporary files kept in {0}".format(self.tmp_dir))

    def ocr(self):
        time_at_start = time.time()
        self.log("Welcome to pdf2pdfocr version {0} - https://github.com/LeoFCardoso/pdf2pdfocr".format(VERSION))
        self.check_avoid_file_by_size()
        self.detect_file_type()
        if self.input_file_type == "application/pdf":
            self.validate_pdf_input_file()
        self.check_rebuild_pdf()
        self.debug("User conversion params: {0}".format(self.user_convert_params))
        self.define_output_files()
        self.initial_cleanup()
        self.convert_input_to_images()
        # TODO - create param to user pass input page range for OCR
        image_file_list = sorted(glob.glob(self.tmp_dir + "{0}*.{1}".format(self.prefix, self.extension_images)))
        if self.input_file_number_of_pages is None:
            self.input_file_number_of_pages = len(image_file_list)
        self.check_avoid_high_pages()
        # TODO - create param to user pass image filters before OCR
        self.check_blank_pages(image_file_list)
        self.autorotate_info(image_file_list)
        self.deskew(image_file_list)
        self.external_ocr(image_file_list)
        if not self.ocr_ignored:
            self.join_ocred_pdf()
            self.create_text_output()
        self.build_final_output()
        self.autorotate_final_output()
        #
        # TODO - create directory watch mode (maybe using watchdog library)
        # Like a daemon
        #
        # TODO - create option for PDF/A files
        # gs -dPDFA=3 -dBATCH -dNOPAUSE -sProcessColorModel=DeviceCMYK -sDEVICE=pdfwrite
        # -sPDFACompatibilityPolicy=2 -sOutputFile=output_filename.pdf ./Test.pdf
        # As in
        # http://git.ghostscript.com/?p=ghostpdl.git;a=blob_plain;f=doc/VectorDevices.htm;hb=HEAD#PDFA
        #
        # Edit producer and build final PDF
        # Without edit producer is easy as "shutil.copyfile(tmp_dir + prefix + "-OUTPUT.pdf", output_file)"
        self.edit_producer()
        #
        self.debug("Output file created")
        #
        # Adjust the new file timestamp
        # TODO touch -r "$INPUT_FILE" "$OUTPUT_FILE"
        #
        self.cleanup()
        time_elapsed = time.time() - time_at_start
        #
        paypal_donate_link = "https://www.paypal.com/cgi-bin/webscr?cmd=_donations&business=leonardo%2ef%2ecardoso%40gmail%2ecom&lc=US&item_name" \
                             "=pdf2pdfocr%20development&currency_code=USD&bn=PP%2dDonationsBF%3abtn_donateCC_LG%2egif%3aNonHosted"
        tippin_donate_link = "https://tippin.me/@LeoFCardoso"
        bitcoin_address = "173D1zQQyzvCCCek9b1SpDvh7JikBEdtRJ"
        dogecoin_address = "D94hD2qPnkxmZk8qa1b6F1d7NfUrPkmcrG"
        pix_key = "0726e8f2-7e59-488a-8abb-bda8f0d7d9ce"
        success_message = """Success in {5:.3f} seconds!
This software is free, but if you like it, please donate to support new features.
---> Paypal
{0}
---> Tippin.me
{1}
---> Bitcoin (BTC) address: {2}
---> Dogecoin (DOGE) address: {3}
---> PIX (Brazilian Instant Payments) key: {4}
---> Please contact for donations in other cryptocurrencies - https://github.com/LeoFCardoso/pdf2pdfocr""".format(
            paypal_donate_link, tippin_donate_link, bitcoin_address, dogecoin_address, pix_key, time_elapsed)
        self.log(success_message)

    def check_rebuild_pdf(self):
        self.rebuild_pdf_from_images = (
                self.input_file_is_encrypted or self.input_file_type != "application/pdf" or self.use_deskew_mode or self.force_rebuild_mode)
        if self.rebuild_pdf_from_images and self.ignore_existing_text:
            self.cleanup()
            raise Pdf2PdfOcrException("Rebuild from images and ignore existing text won't work together")

    def _merge_ocr(self, image_pdf_file_path, text_pdf_file_path, result_pdf_file_path, tag):
        # Merge OCR background PDF into the main PDF document making a PDF sandwich
        self.debug("Merging with OCR")
        if self.path_qpdf is not None:
            try:
                with open(image_pdf_file_path, "rb") as img_f:
                    img_data = PyPDF2.PdfReader(img_f, strict=False)
                    first_page_img_rect = img_data.pages[0].mediaBox
                    first_page_img_area = first_page_img_rect.getWidth() * first_page_img_rect.getHeight()
            except PdfReadError:
                eprint("Warning: could not read input file page geometry. Merge may fail, please check input file.")
                first_page_img_area = 0
            with open(text_pdf_file_path, "rb") as txt_f:
                txt_data = PyPDF2.PdfReader(txt_f, strict=False)
                first_page_txt_rect = txt_data.pages[0].mediaBox
                first_page_txt_area = first_page_txt_rect.getWidth() * first_page_txt_rect.getHeight()
            #
            # Define overlay / underlay based on biggest page
            if first_page_txt_area < first_page_img_area:
                qpdf_command = [self.path_qpdf, "--underlay", image_pdf_file_path, "--", text_pdf_file_path, result_pdf_file_path]
            else:
                qpdf_command = [self.path_qpdf, "--overlay", text_pdf_file_path, "--", image_pdf_file_path, result_pdf_file_path]
            #
            pqpdf = subprocess.Popen(
                qpdf_command,
                stdout=subprocess.DEVNULL,
                stderr=open(self.tmp_dir + "err_merge-qpdf-{0}-{1}.log".format(self.prefix, tag), "wb"),
                shell=self.shell_mode)
            pqpdf.wait()
        else:
            pmulti = subprocess.Popen(
                [self.path_this_python, self.script_dir + 'pdf2pdfocr_multibackground.py',
                 image_pdf_file_path, text_pdf_file_path, result_pdf_file_path],
                stdout=subprocess.DEVNULL,
                stderr=open(self.tmp_dir + "err_merge-multiback-{0}-{1}.log".format(self.prefix, tag), "wb"),
                shell=self.shell_mode)
            pmulti.wait()

    def build_final_output(self):
        # Start building final PDF.
        # First, should we rebuild source file?
        if not self.rebuild_pdf_from_images:
            if not self.ocr_ignored:
                self._merge_ocr(self.input_file, (self.tmp_dir + self.prefix + "-ocr.pdf"), (self.tmp_dir + self.prefix + "-OUTPUT.pdf"),
                                "final-output")
                #
                # Try to handle fail.
                # The code below try to rewrite source PDF and try again.
                if not os.path.isfile(self.tmp_dir + self.prefix + "-OUTPUT.pdf"):
                    self.try_repair_input_and_merge()
            else:
                # OCR ignored
                shutil.copyfile(self.input_file, (self.tmp_dir + self.prefix + "-OUTPUT.pdf"))
        else:
            self.rebuild_and_merge()
        #
        if not os.path.isfile(self.tmp_dir + self.prefix + "-OUTPUT.pdf"):
            self.cleanup()
            raise Pdf2PdfOcrException("Output file could not be created :( Exiting with error code.")

    def rebuild_and_merge(self):
        eprint("Warning: metadata wiped from final PDF file (original file is not an unprotected PDF / "
               "forcing rebuild from extracted images / using deskew)")
        # Convert presets
        # Please read http://www.imagemagick.org/Usage/quantize/#colors_two
        preset_fast = "-threshold 60% -compress Group4"
        preset_best = "-colors 2 -colorspace gray -normalize -threshold 60% -compress Group4"
        preset_grayscale = "-threshold 85% -morphology Dilate Diamond -compress Group4"
        preset_jpeg = "-strip -interlace Plane -gaussian-blur 0.05 -quality 50% -compress JPEG"
        preset_jpeg2000 = "-quality 32% -compress JPEG2000"
        #
        rebuild_list = sorted(glob.glob(self.tmp_dir + self.prefix + "*." + self.extension_images))
        #
        if self.user_convert_params == "smart":
            checkimg_pool_map = self.main_pool.starmap_async(do_check_img_greyscale, zip(rebuild_list))
            checkimg_wait_rounds = 0
            while not checkimg_pool_map.ready() and (self.main_pool is not None):
                checkimg_wait_rounds += 1
                if checkimg_wait_rounds % 10 == 0:
                    self.log("Checking page colors...")
                time.sleep(0.5)
            result_check_img = checkimg_pool_map.get()
            if all(result_check_img):
                self.log("No color pages detected. Smart mode will use 'best' preset.")
                self.user_convert_params = "best"
            else:
                self.log("Color pages detected. Smart mode will use 'jpeg' preset.")
                self.user_convert_params = "jpeg"
        #
        if self.user_convert_params == "fast":
            convert_params = preset_fast
        elif self.user_convert_params == "best":
            convert_params = preset_best
        elif self.user_convert_params == "grayscale":
            convert_params = preset_grayscale
        elif self.user_convert_params == "jpeg":
            convert_params = preset_jpeg
        elif self.user_convert_params == "jpeg2000":
            convert_params = preset_jpeg2000
        else:
            convert_params = self.user_convert_params
        # Handle default case
        if convert_params == "":
            convert_params = preset_best
        #
        self.log("Rebuilding PDF from images")
        rebuild_pool_map = self.main_pool.starmap_async(do_rebuild,
                                                        zip(rebuild_list,
                                                            itertools.repeat(self.path_convert),
                                                            itertools.repeat(convert_params),
                                                            itertools.repeat(self.tmp_dir),
                                                            itertools.repeat(self.shell_mode)))
        rebuild_wait_rounds = 0
        while not rebuild_pool_map.ready() and (self.main_pool is not None):
            rebuild_wait_rounds += 1
            pages_processed = len(glob.glob(self.tmp_dir + "REBUILD_" + self.prefix + "*.pdf"))
            if rebuild_wait_rounds % 10 == 0:
                self.log("Waiting for PDF rebuild to complete. {0}/{1} pages completed...".format(pages_processed, self.input_file_number_of_pages))
            time.sleep(0.5)
        #
        rebuilt_pdf_file_list = sorted(glob.glob(self.tmp_dir + "REBUILD_{0}*.pdf".format(self.prefix)))
        self.debug("We have {0} rebuilt PDF files".format(len(rebuilt_pdf_file_list)))
        if len(rebuilt_pdf_file_list) > 0:
            pdf_merger = PyPDF2.PdfMerger()
            for rebuilt_pdf_file in rebuilt_pdf_file_list:
                pdf_merger.append(PyPDF2.PdfReader(rebuilt_pdf_file, strict=False))
            pdf_merger.write(self.tmp_dir + self.prefix + "-input_unprotected.pdf")
            pdf_merger.close()
        else:
            self.cleanup()
            raise Pdf2PdfOcrException("No PDF files generated after image rebuilding. This is not expected. Aborting.")
        self.debug("PDF rebuilding completed")
        #
        if not self.ocr_ignored:
            self._merge_ocr((self.tmp_dir + self.prefix + "-input_unprotected.pdf"),
                            (self.tmp_dir + self.prefix + "-ocr.pdf"),
                            (self.tmp_dir + self.prefix + "-OUTPUT.pdf"), "rebuild-merge")
        else:
            shutil.copyfile((self.tmp_dir + self.prefix + "-input_unprotected.pdf"), (self.tmp_dir + self.prefix + "-OUTPUT.pdf"))

    def try_repair_input_and_merge(self):
        self.debug("Fail to merge source PDF with extracted OCR text. Trying to fix source PDF to build final file...")
        prepair1 = subprocess.Popen(
            [self.path_pdf2ps, self.input_file, self.tmp_dir + self.prefix + "-fixPDF.ps"],
            stdout=subprocess.DEVNULL,
            stderr=open(self.tmp_dir + "err_pdf2ps-{0}.log".format(self.prefix), "wb"),
            shell=self.shell_mode)
        prepair1.wait()
        prepair2 = subprocess.Popen([self.path_ps2pdf, self.tmp_dir + self.prefix + "-fixPDF.ps",
                                     self.tmp_dir + self.prefix + "-fixPDF.pdf"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=open(self.tmp_dir + "err_ps2pdf-{0}.log".format(self.prefix),
                                                "wb"), shell=self.shell_mode)
        prepair2.wait()
        #
        self._merge_ocr((self.tmp_dir + self.prefix + "-fixPDF.pdf"),
                        (self.tmp_dir + self.prefix + "-ocr.pdf"),
                        (self.tmp_dir + self.prefix + "-OUTPUT.pdf"), "repair_input")

    def create_text_output(self):
        # Create final text output
        if self.create_text_mode:
            text_files = sorted(glob.glob(self.tmp_dir + self.prefix + "*.txt"))
            text_io_wrapper = open(self.output_file_text, 'wb')
            with text_io_wrapper as outfile:
                for fname in text_files:
                    with open(fname, 'rb') as infile:
                        outfile.write(infile.read())
            #
            text_io_wrapper.close()
            #
            self.log("Created final text file")

    def join_ocred_pdf(self):
        # Join PDF files into one file that contains all OCR "backgrounds"
        text_pdf_file_list = sorted(glob.glob(self.tmp_dir + "{0}*.{1}".format(self.prefix, "pdf")))
        self.debug("We have {0} ocr'ed files".format(len(text_pdf_file_list)))
        if len(text_pdf_file_list) > 0:
            pdf_merger = PyPDF2.PdfMerger()
            for text_pdf_file in text_pdf_file_list:
                pdf_merger.append(PyPDF2.PdfReader(text_pdf_file, strict=False))
            pdf_merger.write(self.tmp_dir + self.prefix + "-ocr.pdf")
            pdf_merger.close()
        else:
            self.cleanup()
            raise Pdf2PdfOcrException("No PDF files generated after OCR. This is not expected. Aborting.")
        #
        self.debug("Joined ocr'ed PDF files")

    def external_ocr(self, image_file_list):
        if self.ocr_engine in ["cuneiform", "tesseract"]:
            self.log("Starting OCR with {0}...".format(self.ocr_engine))
            image_list_for_external_ocr = [x for x in image_file_list if x not in self.blank_pages]
            if self.ocr_engine == "cuneiform":
                ocr_pool_map = self.main_pool.starmap_async(do_ocr_cuneiform,
                                                            zip(image_list_for_external_ocr,
                                                                itertools.repeat(self.extra_ocr_flag),
                                                                itertools.repeat(self.tess_langs),
                                                                itertools.repeat(self.tmp_dir),
                                                                itertools.repeat(self.shell_mode),
                                                                itertools.repeat(self.path_cuneiform)))
            elif self.ocr_engine == "tesseract":
                ocr_pool_map = self.main_pool.starmap_async(do_ocr_tesseract,
                                                            zip(image_list_for_external_ocr,
                                                                itertools.repeat(self.extra_ocr_flag),
                                                                itertools.repeat(self.tess_langs),
                                                                itertools.repeat(self.tess_psm),
                                                                itertools.repeat(self.tmp_dir),
                                                                itertools.repeat(self.shell_mode),
                                                                itertools.repeat(self.path_tesseract),
                                                                itertools.repeat(self.text_generation_strategy),
                                                                itertools.repeat(self.delete_temps),
                                                                itertools.repeat(self.tesseract_can_textonly_pdf)))
            else:
                ocr_pool_map = None  # Should never happen
            #
            ocr_rounds = 0
            while not ocr_pool_map.ready() and (self.main_pool is not None):
                ocr_rounds += 1
                pages_processed = len(glob.glob(self.tmp_dir + self.prefix + "*.tmp"))
                if ocr_rounds % 10 == 0:
                    self.log("Waiting for OCR to complete. {0}/{1} pages completed...".format(pages_processed, self.input_file_number_of_pages))
                time.sleep(0.5)
            #
            if len(self.blank_pages) > 0:
                for idx, blank_page_img in enumerate(self.blank_pages):
                    blank_page_dimension = self.blank_pages_dimensions[idx]
                    pdf_file_img = blank_page_img.replace("." + self.extension_images, ".pdf")
                    do_create_blank_pdf(pdf_file_img, blank_page_dimension, self.image_resolution)
            #
            self.log("OCR completed")
            self.ocr_ignored = False
        else:
            self.log("OCR ignored")
            self.ocr_ignored = True

    def check_blank_pages(self, image_file_list):
        self.log("Checking blank pages")
        colors_size_pool_map = self.main_pool.starmap_async(do_check_img_colors_size, zip(image_file_list))
        while not colors_size_pool_map.ready() and (self.main_pool is not None):
            time.sleep(0.5)
        blank_map_values = colors_size_pool_map.get()
        for idx, t_image in enumerate(image_file_list):
            color_size_info = blank_map_values[idx]
            is_blank = (color_size_info[0] is not None) and (len(color_size_info[0]) == 1)
            if is_blank:
                self.blank_pages.append(t_image)
                self.blank_pages_dimensions.append(color_size_info[1])

    def autorotate_info(self, image_file_list):
        if self.use_autorotate:
            self.debug("Calculating autorotate values...")
            image_list_for_autorotate_info = [x for x in image_file_list if x not in self.blank_pages]
            autorotate_pool_map = self.main_pool.starmap_async(do_autorotate_info,
                                                               zip(image_list_for_autorotate_info,
                                                                   itertools.repeat(self.shell_mode),
                                                                   itertools.repeat(self.tmp_dir),
                                                                   itertools.repeat(self.tess_langs),
                                                                   itertools.repeat(self.path_tesseract),
                                                                   itertools.repeat(self.tesseract_version)))
            autorotate_rounds = 0
            while not autorotate_pool_map.ready() and (self.main_pool is not None):
                autorotate_rounds += 1
                pages_processed = len(glob.glob(self.tmp_dir + self.prefix + "*.osd"))
                if autorotate_rounds % 10 == 0:
                    self.log("Waiting for autorotate. {0}/{1} pages completed...".format(pages_processed, self.input_file_number_of_pages))
                time.sleep(0.5)
            #

    def autorotate_final_output(self):
        param_source_file = self.tmp_dir + self.prefix + "-OUTPUT.pdf"
        param_dest_file = self.tmp_dir + self.prefix + "-OUTPUT-ROTATED.pdf"
        # method "autorotate_info" generated these OSD files
        list_osd = sorted(glob.glob(self.tmp_dir + "{0}*.{1}".format(self.prefix, "osd")))
        skip_autorotate = False
        if self.use_autorotate and (len(list_osd) != self.input_file_number_of_pages):
            eprint("Skipping autorotation because OSD files were not correctly generated. Check input file and "
                   "tesseract logs")
            skip_autorotate = True
        #
        if self.use_autorotate and not skip_autorotate:
            self.debug("Autorotate final output")
            file_source = open(param_source_file, 'rb')
            pre_output_pdf = PyPDF2.PdfReader(file_source, strict=False)
            final_output_pdf = PyPDF2.PdfWriter()
            rotation_angles = []
            osd_page_num = 0
            for osd_information_file in list_osd:
                with open(osd_information_file, 'r') as f:
                    osd_information_string = '[root]\n' + f.read()  # A dummy section to satisfy ConfigParser
                f.close()
                osd_page_num += 1
                config_osd = configparser.ConfigParser()
                config_osd.read_file(io.StringIO(osd_information_string))
                try:
                    rotate_value = config_osd.getint('root', 'Rotate')
                except configparser.NoOptionError:
                    self.log("WARN: error reading rotate page value from page {0}. Assuming zero as rotation angle.".format(osd_page_num))
                    rotate_value = 0
                rotation_angles.append(rotate_value)
            #
            for i in range(len(pre_output_pdf.pages)):
                page = pre_output_pdf.pages[i]
                page.rotateClockwise(rotation_angles[i])
                final_output_pdf.addPage(page)
            #
            with open(param_dest_file, 'wb') as f:
                final_output_pdf.write(f)
                f.close()
            #
            file_source.close()
        else:
            # No autorotate, just rename the file to next method process correctly
            self.debug("Autorotate skipped")
            os.rename(param_source_file, param_dest_file)

    def deskew(self, image_file_list):
        if self.use_deskew_mode:
            self.debug("Applying deskew (will rebuild final PDF file)")
            image_list_for_deskew = [x for x in image_file_list if x not in self.blank_pages]
            deskew_pool_map = self.main_pool.starmap_async(do_deskew, zip(image_list_for_deskew, itertools.repeat(self.deskew_threshold),
                                                                          itertools.repeat(self.shell_mode), itertools.repeat(self.path_mogrify)))
            deskew_wait_rounds = 0
            while not deskew_pool_map.ready() and (self.main_pool is not None):
                deskew_wait_rounds += 1
                # noinspection PyProtectedMember
                pages_processed = len([x for x in deskew_pool_map._value if x is not None])
                if deskew_wait_rounds % 10 == 0:
                    self.log("Waiting for deskew to complete. {0}/{1} pages completed...".format(pages_processed, self.input_file_number_of_pages))
                time.sleep(0.5)

    def convert_input_to_images(self):
        self.log("Converting input file to images...")
        if self.input_file_type == "application/pdf":
            input_file_for_images = self.input_file
            if self.ignore_existing_text:
                input_file_for_images = self.tmp_dir + "_" + self.prefix + "-input_file_for_images.pdf"
                # Credits for Kurt Pfeifle: https://stackoverflow.com/questions/24322338/remove-all-text-from-pdf-file
                p_ignore_text = subprocess.Popen([self.path_gs, "-o", input_file_for_images, "-sDEVICE=pdfwrite", "-dFILTERTEXT", self.input_file],
                                                 stdout=subprocess.DEVNULL,
                                                 stderr=open(self.tmp_dir + "err_input_file_for_images-{0}.log".format(self.prefix),
                                                             "wb"), shell=self.shell_mode)
                p_ignore_text.wait()
            #
            parallel_page_ranges = self.calculate_ranges()
            if parallel_page_ranges is not None:
                # TODO - try to use method inside this class (encapsulate do_pdftoimage)
                do_pdftoimage_result_codes = self.main_pool.starmap(do_pdftoimage, zip(itertools.repeat(self.path_pdftoppm),
                                                                                       parallel_page_ranges,
                                                                                       itertools.repeat(input_file_for_images),
                                                                                       itertools.repeat(self.image_resolution),
                                                                                       itertools.repeat(self.tmp_dir),
                                                                                       itertools.repeat(self.prefix),
                                                                                       itertools.repeat(self.shell_mode)))
            else:
                # Without page info, only alternative is going sequentialy (without range)
                do_pdftoimage_result_code = do_pdftoimage(self.path_pdftoppm, None, input_file_for_images, self.image_resolution, self.tmp_dir,
                                                          self.prefix, self.shell_mode)
                do_pdftoimage_result_codes = [do_pdftoimage_result_code]
            #
            if not all(ret_code == 0 for ret_code in do_pdftoimage_result_codes):
                self.cleanup()
                raise Pdf2PdfOcrException("Fail to create images from PDF. Exiting.")
        else:
            if self.input_file_type in ["image/tiff", "image/jpeg", "image/png"]:
                # %09d to format files for correct sort
                p = subprocess.Popen([self.path_convert, self.input_file, '-quality', '100', '-scene', '1',
                                      self.tmp_dir + self.prefix + '-%09d.' + self.extension_images],
                                     shell=self.shell_mode)
                p.wait()
            else:
                self.cleanup()
                raise Pdf2PdfOcrException("{0} is not supported in this script. Exiting.".format(self.input_file_type))

    def initial_cleanup(self):
        Pdf2PdfOcr.best_effort_remove(self.output_file)
        if self.create_text_mode:
            Pdf2PdfOcr.best_effort_remove(self.output_file_text)

    def define_output_files(self):
        if self.force_out_file_mode:
            self.output_file = self.force_out_file
        else:
            if self.force_out_dir_mode:
                output_dir = os.path.abspath(self.force_out_dir)
            else:
                output_dir = os.path.dirname(self.input_file)
            output_name_no_ext = os.path.splitext(os.path.basename(self.input_file))[0]
            self.output_file = output_dir + os.path.sep + output_name_no_ext + "-OCR.pdf"
        #
        self.output_file_text = self.output_file + ".txt"
        self.debug("Output file: {0} for PDF and {1} for TXT".format(self.output_file, self.output_file_text))
        if (self.safe_mode and os.path.isfile(self.output_file)) or \
                (self.safe_mode and self.create_text_mode and os.path.isfile(self.output_file_text)):
            self.cleanup()
            if os.path.isfile(self.output_file):
                raise Pdf2PdfOcrException("{0} already exists and safe mode is enabled. Exiting.".format(self.output_file))
            if self.create_text_mode and os.path.isfile(self.output_file_text):
                raise Pdf2PdfOcrException("{0} already exists and safe mode is enabled. Exiting.".format(self.output_file_text))

    def validate_pdf_input_file(self):
        try:
            pdf_file_obj = open(self.input_file, 'rb')
            pdf_reader = PyPDF2.PdfReader(pdf_file_obj, strict=False)
        except PdfReadError:
            self.cleanup()
            raise Pdf2PdfOcrException("Corrupted PDF file detected. Aborting...")
        #
        try:
            self.input_file_number_of_pages = len(pdf_reader.pages)
        except Exception:
            eprint("Warning: could not read input file number of pages.")
            self.input_file_number_of_pages = None  # Will be calculated later based on number of image files to process
        #
        self.check_avoid_high_pages()
        #
        self.input_file_is_encrypted = pdf_reader.is_encrypted
        if not self.input_file_is_encrypted:
            self.input_file_metadata = pdf_reader.metadata
        #
        if self.check_text_mode:
            self.input_file_has_text = self.check_for_text()
        #
        if self.input_file_type == "application/pdf" and self.check_text_mode and self.input_file_has_text:
            self.cleanup()
            raise Pdf2PdfOcrException("{0} already has text and check text mode is enabled. Exiting.".format(self.input_file))
        #
        if self.input_file_type == "application/pdf" and self.check_protection_mode and self.input_file_is_encrypted:
            self.cleanup()
            raise Pdf2PdfOcrException("{0} is encrypted PDF and check encryption mode is enabled. Exiting.".format(self.input_file))

    def check_avoid_high_pages(self):
        if self.input_file_number_of_pages is not None and self.avoid_high_pages_mode \
                and self.input_file_number_of_pages > self.avoid_high_pages_pages:
            self.cleanup()
            raise Pdf2PdfOcrException("Input file has {0} pages and maximum for process in avoid high number of pages mode (-b) is {1}. "
                                      "Exiting.".format(self.input_file_number_of_pages, self.avoid_high_pages_pages))

    def check_avoid_file_by_size(self):
        if self.avoid_small_file_mode:
            input_file_size_kb = os.path.getsize(self.input_file) / 1024
            if input_file_size_kb < self.avoid_small_file_limit_kb:
                self.cleanup()
                raise Pdf2PdfOcrException("Input file has {0:.2f} KBytes and minimum size to process (--min-kbytes) is {1:.2f} KBytes. "
                                          "Exiting.".format(input_file_size_kb, self.avoid_small_file_limit_kb))

    def check_for_text(self):
        """Check if input file contains text. Actually based on pdffonts from poppler"""
        ptext = subprocess.Popen([self.path_pdffonts, self.input_file], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, shell=self.shell_mode)
        ptext_output, ptext_errors = ptext.communicate()
        ptext.wait()
        pdffonts_text_output_lines = ptext_output.decode("utf-8").strip().splitlines()
        # Return without fonts has exactly 2 header lines.
        # All return with more than 2 lines should mean we have some font (text) in the file.
        if len(pdffonts_text_output_lines) > 2:
            return True
        else:
            return False

    def detect_file_type(self):
        """Detect mime type of input file"""
        pfile = subprocess.Popen([self.path_file, '-b', '--mime-type', self.input_file], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, shell=self.shell_mode)
        pfile_output, pfile_errors = pfile.communicate()
        pfile.wait()
        self.input_file_type = pfile_output.decode("utf-8").strip()
        self.log("Input file {0}: type is {1}".format(self.input_file, self.input_file_type))

    def test_convert(self):
        """
        test convert command to check if it's ImageMagick
        :return: True if it's ImageMagicks convert, false with any other case or error
        """
        try:
            result = False
            test_image = self.tmp_dir + "converttest-" + self.prefix + ".jpg"
            ptest = subprocess.Popen([self.path_convert, 'rose:', test_image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     shell=self.shell_mode)
            ptest.wait()
            return_code = ptest.returncode
            if (return_code == 0) and (os.path.isfile(test_image)):
                Pdf2PdfOcr.best_effort_remove(test_image)
                result = True
            return result
        except Exception:
            self.log("Error testing convert utility. Assuming there is no 'convert' available...")
            return False

    def test_tesseract_textonly_pdf(self):
        result = False
        try:
            result = ('textonly_pdf' in subprocess.check_output([self.path_tesseract, '--print-parameters'], universal_newlines=True))
        except Exception:
            self.log("Error checking tesseract capabilities. Trying to continue without 'textonly_pdf' in Tesseract")
        #
        self.debug("Tesseract can 'textonly_pdf': {0}".format(result))
        return result

    def get_tesseract_version(self):
        # Inspired by the great lib 'pytesseract' - https://github.com/madmaze/pytesseract/blob/master/src/pytesseract.py
        try:
            version_info = subprocess.check_output([self.path_tesseract, '--version'], stderr=subprocess.STDOUT).decode('utf-8').split()
            # self.debug("Tesseract full version info: {0}".format(version_info))
            version_info = version_info[1].lstrip(string.printable[10:])
            l_version_info = parse_version(version_info)
            result = int(l_version_info.base_version.split(".")[0])
            self.debug("Tesseract version: {0}".format(result))
            return result
        except Exception as e:
            self.log("Error checking tesseract version. Trying to continue assuming legacy version 3. Exception was {0}".format(e))
            return 3

    def get_qpdf_version(self):
        try:
            version_info = subprocess.check_output([self.path_qpdf, '--version'], stderr=subprocess.STDOUT).decode('utf-8').split()
            version_info = version_info[2]
            l_version_info = parse_version(version_info)
            self.debug("Qpdf version: {0}".format(l_version_info))
            return l_version_info
        except Exception as e:
            legacy_version = "8.4.0"
            self.log("Error checking qpdf version. Trying to continue assuming legacy version {0}. Exception was {1}".format(legacy_version, e))
            return parse_version(legacy_version)

    def get_pdftoppm_version(self):
        try:
            version_info = subprocess.check_output([self.path_pdftoppm, '-v'], stderr=subprocess.STDOUT).decode('utf-8').split()
            version_info = version_info[2]
            l_version_info = parse_version(version_info)
            self.debug("Pdftoppm version: {0}".format(l_version_info))
            return l_version_info
        except Exception as e:
            legacy_version = "0.70.0"
            self.log("Error checking pdftoppm version. Trying to continue assuming legacy version {0}. Exception was {1}".format(legacy_version, e))
            return parse_version(legacy_version)

    def calculate_ranges(self):
        """
        calculate ranges to run pdftoppm in parallel. Each CPU available will run well defined page range
        :return:
        """
        if (self.input_file_number_of_pages is None) or (self.input_file_number_of_pages < 20):  # 20 to avoid unnecessary parallel operation
            return None
        #
        range_size = math.ceil(self.input_file_number_of_pages / self.cpu_to_use)
        number_of_ranges = math.ceil(self.input_file_number_of_pages / range_size)
        result = []
        for i in range(0, number_of_ranges):
            range_start = (range_size * i) + 1
            range_end = (range_size * i) + range_size
            # Handle last range
            if range_end > self.input_file_number_of_pages:
                range_end = self.input_file_number_of_pages
            result.append((range_start, range_end))
        # Check result
        check_pages = 0
        for created_range in result:
            check_pages += (created_range[1] - created_range[0]) + 1
        if check_pages != self.input_file_number_of_pages:
            raise ArithmeticError("Please check 'calculate_ranges' function, something is wrong...")
        #
        return result

    def edit_producer(self):
        self.debug("Editing producer")
        param_source_file = self.tmp_dir + self.prefix + "-OUTPUT-ROTATED.pdf"
        file_source = open(param_source_file, 'rb')
        pre_output_pdf = PyPDF2.PdfReader(file_source, strict=False)
        final_output_pdf = PyPDF2.PdfWriter()
        for i in range(len(pre_output_pdf.pages)):
            page = pre_output_pdf.pages[i]
            final_output_pdf.addPage(page)
        info_dict_output = dict()
        # Our signature as a producer
        our_name = "PDF2PDFOCR(github.com/LeoFCardoso/pdf2pdfocr)"
        read_producer = False
        producer_key = "/Producer"
        if self.input_file_metadata is not None:
            for key in self.input_file_metadata:
                value = self.input_file_metadata[key]
                if key == producer_key:
                    if type(value) == ByteStringObject:
                        value = str(value, errors="ignore")
                        value = "".join(filter(lambda x: x in string.printable, value))  # Try to remove unprintable
                    value = value + "; " + our_name
                    read_producer = True
                #
                try:
                    # Check if value can be accepted by pypdf API
                    PyPDF2.generic.createStringObject(value)
                    info_dict_output[key] = value
                except TypeError:
                    # This can happen with some array properties.
                    eprint("Warning: property " + key + " not copied to final PDF")
        #
        if not read_producer:
            info_dict_output[producer_key] = our_name
        #
        final_output_pdf.add_metadata(info_dict_output)
        #
        with open(self.output_file, 'wb') as f:
            final_output_pdf.write(f)
            f.close()
        #
        file_source.close()

    @staticmethod
    def best_effort_remove(filename):
        try:
            os.remove(filename)
        except OSError as e:
            if e.errno != errno.ENOENT:  # errno.ENOENT = no such file or directory
                raise  # re-raise exception if a different error occured


# To be used on signal handling
# noinspection PyUnusedLocal,PyUnusedLocal
def sigint_handler(signum, frame):
    global pdf2ocr
    pdf2ocr.cleanup()
    sys.exit(1)


# -------------
# MAIN
# -------------
if __name__ == '__main__':
    # https://docs.python.org/3/library/multiprocessing.html#multiprocessing-programming
    # See "Safe importing of main module"
    multiprocessing.freeze_support()  # Should make effect only on non-fork systems (Windows)
    #
    # From tesseract docs:
    # "If the tesseract executable was built with multithreading support, it will normally use four CPU cores for the OCR process. While this can be
    # faster for a single image, it gives bad performance if the host computer provides less than four CPU cores or if OCR is made for many images.
    # Only a single CPU core is used with OMP_THREAD_LIMIT=1"
    # As we control number of parallel executions, set this env var for the entire script.
    os.environ['OMP_THREAD_LIMIT'] = '1'
    #
    # Adjust Imagemagick parallel control
    # https://legacy.imagemagick.org/script/resources.php
    os.environ['MAGICK_THREAD_LIMIT'] = '1'
    #
    # Arguments
    parser = argparse.ArgumentParser(
        description=('pdf2pdfocr.py [https://github.com/LeoFCardoso/pdf2pdfocr] version %s (http://semver.org/lang/pt-BR/)' % VERSION),
        formatter_class=argparse.RawTextHelpFormatter)
    requiredNamed = parser.add_argument_group('required arguments')
    requiredNamed.add_argument("-i", dest="input_file", action="store", required=True,
                               help="path for input file or folder")
    #
    parser.add_argument("-c", dest="ocr_engine", action="store", default="tesseract", type=str,
                        help="specify OCR engine (tesseract, cuneiform, no_ocr). "
                             "Use no_ocr to skip OCR (for example, to test -f/-g configurations). "
                             "Default: tesseract")
    parser.add_argument("-s", dest="safe_mode", action="store_true", default=False,
                        help="safe mode. Does not overwrite output [PDF | TXT] OCR file")
    parser.add_argument("-t", dest="check_text_mode", action="store_true", default=False,
                        help="check text mode. Does not process if source PDF already has text")
    parser.add_argument("-a", dest="check_protection_mode", action="store_true", default=False,
                        help="check encryption mode. Does not process if source PDF is protected")
    parser.add_argument("-b", dest="max_pages", action="store", default=None, type=int,
                        help="avoid high number of pages mode. Does not process if number of pages is greater "
                             "than <MAX_PAGES>")
    parser.add_argument("--min-kbytes", dest="min_kbytes", action="store", default=None, type=int,
                        help="avoid small files. Does not process if size of input file is lower than <min-kbytes>")
    parser.add_argument("-f", dest="force_rebuild_mode", action="store_true", default=False,
                        help="force PDF rebuild from extracted images")
    # Escape % wiht %%
    option_g_help = """with image input or '-f', use presets or force parameters when calling 'convert' to build the final PDF file
Examples:
    -g fast -> a fast bitonal file ("-threshold 60%% -compress Group4")
    -g best -> best quality, but bigger bitonal file ("-colors 2 -colorspace gray -normalize -threshold 60%% -compress Group4")
    -g grayscale -> good bitonal file from grayscale documents ("-threshold 85%% -morphology Dilate Diamond -compress Group4")
    -g jpeg -> keep original color image as JPEG ("-strip -interlace Plane -gaussian-blur 0.05 -quality 50%% -compress JPEG")
    -g jpeg2000 -> keep original color image as JPEG2000 ("-quality 32%% -compress JPEG2000")
    -g smart -> try to autodetect colors and use 'jpeg' preset if one color page is detected, otherwise use preset 'best'
    -g="-threshold 60%% -compress Group4" -> direct apply these parameters (DON'T FORGET TO USE EQUAL SIGN AND QUOTATION MARKS)
    Note, without -g, preset 'best' is used"""
    parser.add_argument("-g", dest="convert_params", action="store", default="",
                        help=option_g_help)
    parser.add_argument("-d", dest="deskew_percent", action="store",
                        help="use imagemagick deskew *before* OCR. <DESKEW_PERCENT> should be a percent, e.g. '40%%'")
    parser.add_argument("-u", dest="autorotate", action="store_true", default=False,
                        help="try to autorotate pages using 'psm 0' feature [tesseract only]")
    parser.add_argument("-j", dest="parallel_percent", action="store", type=percentual_float,
                        help="run this percentual jobs in parallel (0 - 1.0] - multiply with the number of CPU cores"
                             " (default = 1 [all cores])")
    parser.add_argument("-w", dest="create_text_mode", action="store_true", default=False,
                        help="also create a text file at same location of PDF OCR file [tesseract only]")
    parser.add_argument("-o", dest="output_file", action="store", required=False,
                        help="path for output file")
    parser.add_argument("-O", dest="output_dir", action="store", required=False,
                        help="path for output directory")
    parser.add_argument("-p", dest="no_effect_01", action="store_true", default=False,
                        help="no effect, do not use (reverse compatibility)")
    parser.add_argument("-r", dest="image_resolution", action="store", default=300, type=int,
                        help="specify image resolution in DPI before OCR operation - lower is faster, higher "
                             "improves OCR quality (default is for quality = 300)")
    parser.add_argument("-e", dest="text_generation_strategy", action="store", default="tesseract", type=str,
                        help="specify how text is generated in final pdf file (tesseract, native) [tesseract only]. Default: tesseract")
    parser.add_argument("-l", dest="tess_langs", action="store", required=False,
                        help="force tesseract or cuneiform to use specific language (default: por+eng)")
    parser.add_argument("-m", dest="tess_psm", action="store", required=False,
                        help="force tesseract to use OCR with specific \"pagesegmode\" (default: tesseract "
                             "OCR default = 1) [tesseract only]. Use with caution")
    parser.add_argument("-x", dest="extra_ocr_flag", action="store", required=False,
                        help="add extra command line flags in select OCR engine for all pages. Use with caution")
    parser.add_argument("--timeout", dest="timeout", action="store", default=None, type=int,
                        help="run with time limit in seconds")
    parser.add_argument("--ignore-existing-text", dest="ignore_existing_text", action="store_true", default=False,
                        help="don't OCR again native PDF text")
    parser.add_argument("-k", dest="keep_temps", action="store_true", default=False,
                        help="keep temporary files for debug")
    parser.add_argument("-v", dest="verbose_mode", action="store_true", default=False,
                        help="enable verbose mode")
    parser.add_argument("-P", dest="pause_end_mode", action="store_true", default=False,
                        help="with successful execution, wait for user to press <Enter> at the final of the "
                             "script (default: not wait)")
    # Dummy to be called by gooey (GUI)
    parser.add_argument("--ignore-gooey", action="store_true", required=False, default=False)
    #
    pdf2ocr_args = parser.parse_args()
    #
    dir_mode = os.path.isdir(pdf2ocr_args.input_file)
    if dir_mode:
        file_to_process_list = []
        for t_root, t_directories, t_files in os.walk(pdf2ocr_args.input_file):
            for name in t_files:
                file_to_process_list.append(os.path.join(t_root, name))
    else:
        file_to_process_list = [pdf2ocr_args.input_file]
    #
    signal.signal(signal.SIGINT, sigint_handler)
    #
    all_success = True
    for file_to_process in file_to_process_list:
        try:
            print("-------------------------------------")
            print("File:", file_to_process, flush=True)
            pdf2ocr = Pdf2PdfOcr(pdf2ocr_args, file_to_process)
            #
            if pdf2ocr_args.timeout:
                # https://stackoverflow.com/questions/56305195/is-it-possible-to-specify-the-max-amount-of-time-to-wait-for-code-to-run-with-py
                # /56305465
                with futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future_pdf2ocr = executor.submit(pdf2ocr.ocr)
                    try:
                        future_pdf2ocr.result(pdf2ocr_args.timeout)
                    except futures.TimeoutError as fte:
                        #
                        # https://stackoverflow.com/questions/48350257/how-to-exit-a-script-after-threadpoolexecutor-has-timed-out
                        import atexit

                        # noinspection PyProtectedMember
                        atexit.unregister(futures.thread._python_exit)
                        executor.shutdown = lambda wait: None
                        #
                        pdf2ocr.cleanup()
                        raise Pdf2PdfOcrException("Script stopped due to timeout of {0} seconds".format(pdf2ocr_args.timeout))
            else:
                pdf2ocr.ocr()
        except Pdf2PdfOcrException as e_p:
            print("Error:", e_p, flush=True)
            all_success = False
    #
    if pdf2ocr_args.pause_end_mode:
        input("Press <Enter> to continue...")
    #
    if all_success:
        sys.exit(0)
    else:
        sys.exit(1)
    #
# This is the end
