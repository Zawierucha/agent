from __future__ import annotations

import gzip
import importlib.util
import io
import json
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import anndata as ad
import h5py
import numpy as np
import pandas as pd
import pytest
from scipy import io as scipy_io
from scipy import sparse

TOOL_DIR = Path(__file__).resolve().parents[1] / ".agents" / "tools" / "sc_dataset"
sys.path.insert(0, str(TOOL_DIR))

from adapters.base import AdapterContext
from adapters.dense_gene_cell import ADAPTER as DENSE
from adapters.h5ad_passthrough import ADAPTER as H5AD
from adapters.nested_tar_expression import ADAPTER as NESTED
from adapters.tenx_hdf5 import ADAPTER as HDF5
from adapters.tenx_mtx import TenxMatrixMarketAdapter

spec = importlib.util.spec_from_file_location("geo_anndata_load", TOOL_DIR / "geo_anndata_load.py")
loader = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(loader)
MTX = TenxMatrixMarketAdapter()
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "data/anndata_schema.json").read_text())


def _context(root: Path, *, gsms=("GSM123",), report=None, metadata=None, manifest=None) -> AdapterContext:
    return AdapterContext("GSE12345", root, set(gsms), metadata or {}, manifest or {"files": []},
                          report or {"10x_candidate_groups": [{"complete_standard_10x_triplet": True}]},
                          SCHEMA, "schema-hash")


def _write_mtx_triplet(root: Path, prefix="GSM123_ABC1", *, compressed=False, nfeatures=2, ncells=2,
                       feature_columns=3, name_case=None, values=None):
    root.mkdir(parents=True, exist_ok=True)
    names = [f"{prefix}_matrix.mtx", f"{prefix}_barcodes.tsv", f"{prefix}_features.tsv"]
    if name_case == "lower":
        names = [name.lower() for name in names]
    if values is None:
        matrix = sparse.coo_matrix(np.eye(nfeatures, ncells, dtype=np.int32))
    else:
        matrix = sparse.coo_matrix(np.asarray(values, dtype=np.int32))
    stream = io.BytesIO()
    scipy_io.mmwrite(stream, matrix)
    matrix_bytes = stream.getvalue()
    barcode_bytes = "".join(f"BC{i+1}\n" for i in range(ncells)).encode()
    feature_rows=[]
    for i in range(nfeatures):
        fields=[f"ENSG{i+1}", f"Gene {i+1}", "Gene Expression"]
        feature_rows.append("\t".join(fields[:feature_columns]))
    feature_bytes=("\n".join(feature_rows)+"\n").encode()
    payloads=[matrix_bytes, barcode_bytes, feature_bytes]
    if compressed:
        payloads=[gzip.compress(payload) for payload in payloads]
        names=[name+".gz" for name in names]
    for name,payload in zip(names,payloads):
        (root/name).write_bytes(payload)
    return names, payloads


def _load_mtx(root: Path):
    context=_context(root)
    candidates=MTX.discover(context)
    assert len(candidates)==1
    result=MTX.load(context,candidates[0])
    loader.validate_adata(result, context.canonical_schema)
    return result


def _tar_add(tf, name: str, payload: bytes):
    info=tarfile.TarInfo(name); info.size=len(payload); tf.addfile(info,io.BytesIO(payload))


def test_mtx_loose_uncompressed_triplet(tmp_path: Path):
    _write_mtx_triplet(tmp_path)
    a=_load_mtx(tmp_path)
    assert a.shape==(2,2)


def test_mtx_triplet_in_two_nested_tar_directories(tmp_path: Path):
    archive=tmp_path/"nested.tar.gz"
    _, payloads=_write_mtx_triplet(tmp_path/"payload")
    with tarfile.open(archive,"w:gz") as tf:
        _tar_add(tf,"first/second/GSM123_ABC1_matrix.mtx",payloads[0])
        _tar_add(tf,"first/second/GSM123_ABC1_barcodes.tsv",payloads[1])
        _tar_add(tf,"first/second/GSM123_ABC1_features.tsv",payloads[2])
    shutil.rmtree(tmp_path/"payload")
    a=_load_mtx(tmp_path)
    assert a.shape==(2,2)


def test_mtx_zip_gz_named_member_with_uncompressed_payload(tmp_path: Path):
    archive=tmp_path/"plain.zip"
    names,payloads=_write_mtx_triplet(tmp_path/"payload")
    with zipfile.ZipFile(archive,"w") as zf:
        for name,payload in zip(names,payloads):
            zf.writestr(name+".gz",payload)
    shutil.rmtree(tmp_path/"payload")
    a=_load_mtx(tmp_path)
    assert a.shape==(2,2)


def test_mtx_v2_two_column_features_tsv(tmp_path: Path):
    _write_mtx_triplet(tmp_path, feature_columns=2)
    a=_load_mtx(tmp_path)
    assert a.var["feature_type"].isna().all()


@pytest.mark.parametrize(("nfeatures","ncells"),[(5,1),(1,5)])
def test_mtx_singleton_axis_orientations(tmp_path: Path,nfeatures:int,ncells:int):
    matrix=np.arange(1,nfeatures*ncells+1,dtype=np.int32).reshape(nfeatures,ncells)
    _write_mtx_triplet(tmp_path,nfeatures=nfeatures,ncells=ncells,values=matrix)
    a=_load_mtx(tmp_path)
    assert a.shape==(ncells,nfeatures)


def test_mtx_lowercase_gsm_in_filename(tmp_path: Path):
    _write_mtx_triplet(tmp_path,prefix="gsm123_ABC1",name_case="lower")
    a=_load_mtx(tmp_path)
    assert a.obs["sample_id"].tolist()==["GSM123","GSM123"]


def _write_h5(path: Path, *, group="matrix", layout="v3", barcodes=None):
    with h5py.File(path,"w") as handle:
        g=handle.create_group(group)
        if layout=="invalid":
            g.create_dataset("noise",data=[1,2]); return
        bcs=np.asarray(barcodes if barcodes is not None else [b"BC1",b"BC2"])
        nfeat=2
        g.create_dataset("data",data=np.asarray([1,2,3],dtype=np.int32))
        g.create_dataset("indices",data=np.asarray([0,1,1],dtype=np.int32))
        g.create_dataset("indptr",data=np.asarray([0,2,3],dtype=np.int32))
        g.create_dataset("shape",data=np.asarray((nfeat,len(bcs)),dtype=np.int64))
        g.create_dataset("barcodes",data=bcs)
        if layout=="v2":
            g.create_dataset("gene_names",data=np.asarray([b"Gene1",b"Gene2"]))
            g.create_dataset("gene_ids",data=np.asarray([b"ENSG1",b"ENSG2"]))
        else:
            f=g.create_group("features")
            f.create_dataset("name",data=np.asarray([b"Gene1",b"Gene2"]))
            f.create_dataset("feature_type",data=np.asarray([b"Gene Expression"]*2))
            f.create_dataset("id",data=np.asarray([b"ENSG1",b"ENSG2"]))


def _discover_h5(root):
    context=_context(root,report={})
    return HDF5.discover(context),context


def _load_h5(root):
    candidates,context=_discover_h5(root)
    assert len(candidates)==1
    a=HDF5.load(context,candidates[0]); loader.validate_adata(a,context.canonical_schema)
    return a,candidates[0]


def test_hdf5_v2_group_level_gene_names_and_ids(tmp_path: Path):
    _write_h5(tmp_path/"GSM123_v2.h5",layout="v2")
    a,_=_load_h5(tmp_path)
    assert a.var.index.tolist()==["ENSG1","ENSG2"]
    assert a.var["feature_type"].isna().all()


def test_hdf5_only_raw_group_admitted(tmp_path: Path):
    _write_h5(tmp_path/"GSM123_raw.h5",group="raw")
    candidates,_=_discover_h5(tmp_path)
    assert len(candidates)==1 and candidates[0].components["group"]=="raw"
    a,_=_load_h5(tmp_path); assert a.shape==(2,2)


def test_hdf5_matrix_preferred_when_raw_and_matrix_complete(tmp_path: Path):
    _write_h5(tmp_path/"GSM123_both.h5",group="raw")
    _write_h5(tmp_path/"GSM123_both.h5",group="matrix")
    # Write both groups in one file after initial discovery fixture helper.
    path=tmp_path/"GSM123_both.h5"
    with h5py.File(path,"a") as handle:
        g=handle.create_group("raw")
        g.create_dataset("data",data=[1,2,3]); g.create_dataset("indices",data=[0,1,1]); g.create_dataset("indptr",data=[0,2,3])
        g.create_dataset("shape",data=[2,2]); g.create_dataset("barcodes",data=np.asarray([b"R1",b"R2"]))
        f=g.create_group("features"); f.create_dataset("name",data=[b"R1",b"R2"]); f.create_dataset("feature_type",data=[b"Gene Expression"]*2); f.create_dataset("id",data=[b"R1",b"R2"])
    candidates,_=_discover_h5(tmp_path)
    assert len(candidates)==1 and candidates[0].components["group"]=="matrix"


def test_hdf5_non_10x_returns_no_candidate(tmp_path: Path):
    with h5py.File(tmp_path/"GSM123_other.h5","w") as handle: handle.create_dataset("noise",data=[1,2])
    assert _discover_h5(tmp_path)[0]==[]


def test_hdf5_non_string_barcodes_returns_no_candidate(tmp_path: Path):
    _write_h5(tmp_path/"GSM123_bad.h5",barcodes=np.asarray([1,2],dtype=np.int32))
    assert _discover_h5(tmp_path)[0]==[]


def _h5ad_context(root): return _context(root,report={})


def _source_h5ad(root:Path,x,*,layers=None,obs_index=None,var_index=None):
    obs_index=obs_index if obs_index is not None else ["cell-a","cell-b"]
    var_index=var_index if var_index is not None else ["gene-a","gene-b"]
    obs=pd.DataFrame(index=pd.Index(obs_index,name="obs_name" if obs_index[0]=="named-a" else None))
    var=pd.DataFrame(index=pd.Index(var_index,name="feature_id"))
    source=ad.AnnData(X=x,obs=obs,var=var)
    for key,value in (layers or {}).items(): source.layers[key]=value
    path=root/"GSM123_source.h5ad"; source.write_h5ad(path)
    return path


def _load_h5ad(root):
    context=_h5ad_context(root); candidates=H5AD.discover(context); assert len(candidates)==1
    result=H5AD.load(context,candidates[0]); loader.validate_adata(result,context.canonical_schema)
    return result


def test_h5ad_integer_x_and_source_counts_layer(tmp_path: Path):
    x=sparse.csr_matrix(np.asarray([[1,0],[0,2]],dtype=np.int32))
    a=_load_h5ad(tmp_path) if False else None
    _source_h5ad(tmp_path,x,layers={"counts":x.copy()})
    a=_load_h5ad(tmp_path)
    assert a.uns["matrix_semantics"]=="raw counts" and "counts" in a.layers


def test_h5ad_fractional_x_omits_source_layers_and_notes(tmp_path: Path):
    x=sparse.csr_matrix(np.asarray([[1.25,0],[0,2.5]],dtype=np.float32))
    _source_h5ad(tmp_path,x,layers={"counts":sparse.csr_matrix([[1,0],[0,2]])})
    a=_load_h5ad(tmp_path)
    assert a.uns["matrix_semantics"]=="unknown" and "counts" not in a.layers
    assert "source expression layers were not copied: counts" in a.uns["harmonization_notes"]


def test_h5ad_named_obs_index_is_not_used_as_source_barcode(tmp_path: Path):
    _source_h5ad(tmp_path,sparse.csr_matrix(np.eye(2)),obs_index=["named-a","named-b"])
    a=_load_h5ad(tmp_path)
    assert a.obs["source_barcode"].isna().all()
    assert a.obs.index.tolist()==["GSM123:named-a","GSM123:named-b"]


def test_h5ad_dense_float_x_becomes_csr_float32(tmp_path: Path):
    _source_h5ad(tmp_path,np.asarray([[0.5,0],[0,1.5]],dtype=np.float64))
    a=_load_h5ad(tmp_path)
    assert sparse.isspmatrix_csr(a.X) and a.X.dtype==np.float32


def test_h5ad_duplicated_var_index_fails_contract(tmp_path: Path):
    _source_h5ad(tmp_path,sparse.csr_matrix(np.eye(2)),var_index=["dup","dup"])
    context=_h5ad_context(tmp_path); candidate=H5AD.discover(context)[0]
    result=H5AD.load(context,candidate)
    with pytest.raises(ValueError): loader.validate_adata(result,context.canonical_schema)


def test_dense_csv_gz_with_gene_header(tmp_path: Path):
    path=tmp_path/"GSM123_counts.csv.gz"
    with gzip.open(path,"wt") as out: out.write("Gene,BC1,BC2\nG1,1,0\nG2,0,3\n")
    context=_context(tmp_path,report={}); candidate=DENSE.discover(context)[0]
    a=DENSE.load(context,candidate); loader.validate_adata(a,context.canonical_schema)
    assert a.shape==(2,2) and "counts" in a.layers


def test_dense_accepts_uppercase_gene_name_header(tmp_path: Path):
    path=tmp_path/"GSM123_counts.csv"; path.write_text("GENE.NAME,BC1,BC2\nG1,1,0\nG2,0,3\n")
    context=_context(tmp_path,report={}); candidate=DENSE.discover(context)[0]
    a=DENSE.load(context,candidate); loader.validate_adata(a,context.canonical_schema)
    assert a.shape==(2,2)


@pytest.mark.parametrize(("text","message"),[
    ("gene\tBC1\tBC2\nG1\t1\n","wrong number"),
    ("gene\tBC1\tBC2\nG1\t-1\t0\n","negative"),
    ("gene\tBC1\tBC2\nG1\tnope\t0\n","non-numeric"),
])
def test_dense_invalid_rows_raise(tmp_path: Path,text:str,message:str):
    path=tmp_path/"GSM123_counts.tsv"; path.write_text(text)
    context=_context(tmp_path,report={}); candidate=DENSE.discover(context)[0]
    with pytest.raises(ValueError,match=message): DENSE.load(context,candidate)


def test_dense_missing_gsm_prefix_not_discovered(tmp_path: Path):
    (tmp_path/"counts.tsv").write_text("gene\tBC1\tBC2\nG1\t1\t0\n")
    assert DENSE.discover(_context(tmp_path,report={}))==[]


def _nested_outer(root:Path,*,matrix_text:bytes|None=None,include_features=True):
    root.mkdir(parents=True,exist_ok=True)
    inner=io.BytesIO()
    with tarfile.open(fileobj=inner,mode="w:gz") as tf:
        files={"matrix.mtx.gz":gzip.compress(matrix_text or b"%%MatrixMarket matrix coordinate integer general\n2 2 2\n1 1 1\n2 2 2\n"),
               "barcodes.tsv.gz":gzip.compress(b"BC1\nBC2\n")}
        if include_features: files["features.tsv.gz"]=gzip.compress(b"ENSG1\tGene1\tGene Expression\nENSG2\tGene2\tGene Expression\n")
        for name,data in files.items(): _tar_add(tf,name,data)
    outer=root/"outer.tar.gz"
    with tarfile.open(outer,"w:gz") as tf: _tar_add(tf,"GSM123_sample.tgz",inner.getvalue())
    return outer


def _nested_candidate(root):
    context=_context(root,report={}); candidates=NESTED.discover(context)
    return candidates,context


def test_nested_tar_gz_per_gsm_triplet_raw_counts(tmp_path: Path):
    _nested_outer(tmp_path)
    candidates,context=_nested_candidate(tmp_path); assert len(candidates)==1
    a=NESTED.load(context,candidates[0]); loader.validate_adata(a,context.canonical_schema)
    assert a.uns["matrix_semantics"]=="raw counts" and "counts" in a.layers


def test_nested_tar_noninteger_mtx_load_raises(tmp_path: Path):
    _nested_outer(tmp_path,matrix_text=b"%%MatrixMarket matrix coordinate real general\n2 2 2\n1 1 1.5\n2 2 2.5\n")
    candidates,context=_nested_candidate(tmp_path); assert len(candidates)==1
    with pytest.raises(ValueError,match="non-integer"): NESTED.load(context,candidates[0])


def test_nested_tar_missing_features_not_discovered(tmp_path: Path):
    _nested_outer(tmp_path,include_features=False)
    assert _nested_candidate(tmp_path)[0]==[]
