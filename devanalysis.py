import itertools
import logging
import traceback
from math import sqrt
from typing import Tuple, Any, Optional, List

import matplotlib.pyplot as plt
import pandas as pd
from elasticsearch import Elasticsearch
from matplotlib.figure import Figure
from statsmodels.tsa.api import VAR
from statsmodels.tsa.seasonal import seasonal_decompose, DecomposeResult
from statsmodels.tsa.stattools import adfuller
from statsmodels.tsa.vector_ar.hypothesis_test_results import CausalityTestResults, WhitenessTestResults, \
    NormalityTestResults
from statsmodels.tsa.vector_ar.irf import IRAnalysis
from statsmodels.tsa.vector_ar.var_model import VARResults, LagOrderResults

from aggregation import MERGES_PERFORMED_COLUMN, MERGES_SUCCESSFUL_COLUMN, get_merges_performed, get_merge_requests, \
    get_requests_merged, get_all_mergers, MERGES_REQUESTED_COLUMN

IMAGE_DIRECTORY: str = "img/"
TEXT_DIRECTORY: str = "txt/"


def plot_dataframe(consolidated_dataframe: pd.DataFrame, plot_title: str) -> None:
    plt.rcParams['figure.figsize'] = (20, 10)
    plt.style.use('fivethirtyeight')
    _ = consolidated_dataframe.plot(subplots=True, linewidth=2, fontsize=12, title=plot_title)
    plt.savefig(IMAGE_DIRECTORY + plot_title + ".png")


def check_stationarity(consolidated_dataframe: pd.DataFrame, user_login: str, data_column: str,
                       threshold: float = 0.05) -> bool:
    # noinspection PyTypeChecker
    test_result: list[float] = adfuller(consolidated_dataframe[data_column])
    adf_statistic: float = test_result[0]
    p_value: float = test_result[1]
    if p_value <= threshold:
        print("%s is stationary for user %s. ADF statistic: %f, p-value: %f" % (
            data_column, user_login, adf_statistic, p_value))
        return True

    print("%s is NOT stationary for user %s. ADF statistic: %f, p-value: %f" % (
        data_column, user_login, adf_statistic, p_value))
    return True


def check_causality(variables: Tuple, training_result: VARResults, user_login: str, permutation_index: int,
                    causality_threshold=0.05) -> dict[str, bool]:
    test_results: dict[str, Any] = {}
    for cause_data_column in variables:
        for effect_data_column in variables:
            if cause_data_column != effect_data_column:
                causality_results: CausalityTestResults = training_result.test_causality(causing=cause_data_column,
                                                                                         caused=effect_data_column,
                                                                                         kind='wald',
                                                                                         signif=causality_threshold)

                with open(TEXT_DIRECTORY + "user_{}_permutation_{}_analysis_results.txt".format(user_login,
                                                                                                permutation_index),
                          "a") as file:
                    file.write(str(causality_results.summary()) + "\n")

                granger_causality: bool = causality_results.conclusion == "reject"
                test_results[cause_data_column + "->" + effect_data_column] = granger_causality
                print(causality_results.summary())

    return test_results


def get_lags_for_whiteness_test(user_login: str, sample_size: int, candidate_order) -> int:
    lags_for_whiteness: int = max(round(sqrt(sample_size)), candidate_order + 1)
    logging.info(
        "User {}: Portmanteau test using lags {} for VAR({}) and {} samples".format(user_login, lags_for_whiteness,
                                                                                    candidate_order, sample_size))
    return lags_for_whiteness


def fit_var_model(var_model: VAR, information_criterion: str, user_login: str, sample_size: int) -> Tuple[
    VARResults, WhitenessTestResults, NormalityTestResults, LagOrderResults]:
    order_results: LagOrderResults = var_model.select_order()
    candidate_order: int = order_results.selected_orders[information_criterion]

    training_result: VARResults = var_model.fit(maxlags=candidate_order)
    whiteness_result: WhitenessTestResults = training_result.test_whiteness(
        nlags=get_lags_for_whiteness_test(user_login, sample_size, candidate_order))
    normality_result: NormalityTestResults = training_result.test_normality()

    while whiteness_result.conclusion == "reject" and candidate_order <= 12:
        candidate_order += 1
        logging.warning("ALERT! Serial correlation in residuals for user %s. Increasing lag order to %d" % (
            user_login, candidate_order))
        training_result: VARResults = var_model.fit(maxlags=candidate_order)
        whiteness_result: WhitenessTestResults = training_result.test_whiteness(
            nlags=get_lags_for_whiteness_test(user_login, sample_size, candidate_order))
        normality_result: NormalityTestResults = training_result.test_normality()

    print(training_result.summary())
    print(whiteness_result.summary())
    print(normality_result.summary())

    return training_result, whiteness_result, normality_result, order_results


def do_structural_analysis(variables: Tuple, training_result: VARResults, periods: int,
                           user_login: str, project: str, calendar_interval: str, permutation_index: int) -> dict[
    str, bool]:
    causality_results: dict[str, bool] = {}
    try:
        causality_results = check_causality(variables, training_result, user_login, permutation_index)

        impulse_response: IRAnalysis = training_result.irf(periods=periods)
        impulse_response.plot(figsize=(15, 15))
        plt.savefig(IMAGE_DIRECTORY + "%s_%s_impulse_response_%s_%i.png" % (
            user_login, project, calendar_interval, permutation_index))
        impulse_response.plot_cum_effects(figsize=(15, 15))
        plt.savefig(IMAGE_DIRECTORY + "%s_%s_cumulative_response_%s_%i.png" % (
            user_login, project, calendar_interval, permutation_index))

        variance_decomposition = training_result.fevd(periods=periods)
        variance_decomposition.plot(figsize=(15, 15))
        plt.savefig(IMAGE_DIRECTORY + "%s_%s_variance_decomposition_%s_%i.png" % (
            user_login, project, calendar_interval, permutation_index))

    except Exception:
        logging.error(traceback.format_exc())
        logging.error("Cannot do structural analysis for user %s" % user_login)
    finally:
        return causality_results


def train_var_model(consolidated_dataframe: pd.DataFrame, user_login: str, variables: Tuple, project: str,
                    calendar_interval: str, information_criterion='bic', periods=24) -> dict[str, set]:
    test_observations: int = 6
    train_dataset: pd.DataFrame = consolidated_dataframe[:-test_observations]
    test_dataset: pd.DataFrame = consolidated_dataframe[-test_observations:]
    train_sample_size: int = len(train_dataset)
    print("%s Train data: %d Test data: %d" % (user_login, train_sample_size, len(test_dataset)))

    var_order_key: str = "var_order"
    serial_correlation_key: str = "serial_correlation"
    residual_white_noise_key: str = "residual_white_noise"

    result_analysis: dict[str, Any] = {
        "train_sample_size": [train_sample_size],
        var_order_key: set(),
        serial_correlation_key: set(),
        residual_white_noise_key: set()
    }

    for permutation_index, permutation in enumerate(itertools.permutations(variables)):
        print("Permutation %d : %s" % (permutation_index, str(permutation)))
        train_dataset: pd.DataFrame = train_dataset[list(permutation)]

        var_model: VAR = VAR(train_dataset)
        training_result, whiteness_result, normality_result, var_order_result = fit_var_model(var_model,
                                                                                              information_criterion,
                                                                                              user_login,
                                                                                              train_sample_size)

        user_report_file: str = TEXT_DIRECTORY + "user_{}_permutation_{}_analysis_results.txt".format(user_login,
                                                                                                      permutation_index)
        with open(user_report_file, "a") as file:
            file.truncate()
            file.write(str(var_order_result.summary()) + "\n")
            file.write(str(training_result.summary()) + "\n")
            file.write(str(whiteness_result.summary()) + "\n")
            file.write(str(normality_result.summary()) + "\n")

        result_analysis[var_order_key].add(training_result.k_ar)

        if whiteness_result.conclusion == "reject":
            logging.error("ALERT! Serial correlation found in the residuals for user %s" % user_login)
            result_analysis[serial_correlation_key].add(True)
        else:
            result_analysis[serial_correlation_key].add(False)

        if normality_result.conclusion == "reject":
            logging.error("ALERT! Residuals are NOT Gaussian white noise for user %s" % user_login)
            result_analysis[residual_white_noise_key].add(False)
        else:
            result_analysis[residual_white_noise_key].add(True)

        causality_results: dict[str, bool] = do_structural_analysis(variables, training_result, periods, user_login,
                                                                    project, calendar_interval, permutation_index)

        for test, result in causality_results.items():
            if test in result_analysis:
                result_analysis[test].add(result)
            else:
                result_analysis[test] = set()
                result_analysis[test].add(result)

    return result_analysis


def plot_seasonal_decomposition(consolidated_dataframe: pd.DataFrame, user_login: str,
                                project: str,
                                column: str = MERGES_PERFORMED_COLUMN) -> None:
    merges_performed_decomposition: DecomposeResult = seasonal_decompose(
        consolidated_dataframe[column],
        model='additive')

    _: Figure = merges_performed_decomposition.plot()
    plt.savefig(IMAGE_DIRECTORY + "%s_%s_seasonal_decomposition_%s.png" % (user_login, project, column))


def consolidate_dataframe(es: Elasticsearch, pull_request_index: str, user_login: str, variables: Tuple,
                          calendar_interval: str) -> pd.DataFrame:
    data: list[pd.DataFrame] = []
    if MERGES_PERFORMED_COLUMN in variables:
        merges_performed_dataframe: pd.DataFrame = get_merges_performed(es, pull_request_index, user_login,
                                                                        calendar_interval)
        if not len(merges_performed_dataframe):
            logging.error("User %s does not merge PRs for other developers" % user_login)
            return pd.DataFrame()

        data.append(merges_performed_dataframe)

    if MERGES_REQUESTED_COLUMN in variables:
        merge_requests_dataframe: pd.DataFrame = get_merge_requests(es, pull_request_index, user_login,
                                                                    calendar_interval)
        data.append(merge_requests_dataframe)

    if MERGES_SUCCESSFUL_COLUMN in variables:
        requests_merged_dataframe: pd.DataFrame = get_requests_merged(es, pull_request_index, user_login,
                                                                      calendar_interval)
        if not len(requests_merged_dataframe):
            logging.error("User %s does not have PRs merged by other developers" % user_login)
            return pd.DataFrame()

        data.append(requests_merged_dataframe)

    consolidated_dataframe: pd.DataFrame = pd.concat(data, axis=1)
    consolidated_dataframe = consolidated_dataframe.fillna(0)
    consolidated_dataframe = consolidated_dataframe.rename_axis('metric', axis=1)
    return consolidated_dataframe


def analyse_user(es: Elasticsearch, pull_request_index: str, user_login: str, variables: Tuple, calendar_interval: str,
                 information_criterion: str,
                 project: str) -> Optional[dict[str, Any]]:
    consolidated_dataframe = consolidate_dataframe(es, pull_request_index, user_login, variables, calendar_interval)
    data_points: int = len(consolidated_dataframe)
    if not len(consolidated_dataframe):
        print("No data points for user %s on index %s" % (user_login, pull_request_index))
        return None

    print("Data points for user %s: %d. Calendar interval: %s" % (user_login, data_points, calendar_interval))

    analysis_result: dict[str, Any] = {
        "user_login": user_login,
        "data_points": data_points,
        "index": pull_request_index
    }

    for column in variables:
        analysis_result[column] = consolidated_dataframe[column].sum()

    after_differencing_data = consolidated_dataframe.diff()
    after_differencing_data = after_differencing_data.dropna()
    print("Applying 1st order differencing to the data")

    for variable in variables:
        is_stationary: bool = check_stationarity(after_differencing_data, user_login, variable)
        if not is_stationary:
            logging.error("ALERT! %s is not stationary" % variable)
            analysis_result[variable + "_stationary"] = False
        else:
            analysis_result[variable + "_stationary"] = True

    var_results: dict[str, Any] = train_var_model(after_differencing_data[list(variables)], user_login, variables,
                                                  project, calendar_interval,
                                                  information_criterion=information_criterion)

    try:
        plot_dataframe(consolidated_dataframe, "%s_%s_before_differencing" % (user_login, project))
        plot_dataframe(after_differencing_data, "%s_%s_after_differencing" % (user_login, project))
        plot_seasonal_decomposition(consolidated_dataframe, user_login, project)
    except ValueError:
        logging.error(traceback.format_exc())
        logging.error("Error while building diagnosis plots for user %s" % user_login)

    for key, values in var_results.items():
        analysis_result[key] = " ".join([str(value) for value in values])

    return analysis_result


def analyse_project(es: Elasticsearch, pull_request_index: str, calendar_interval: str, variables: Tuple,
                    information_criterion: str) -> Tuple[int, pd.DataFrame]:
    es.indices.refresh(index=pull_request_index)
    # noinspection PyTypeChecker
    document_count: List[dict] = es.cat.count(index=pull_request_index, params={"format": "json"})
    documents: int = int(document_count[0]['count'])
    print("Documents on index %s: %s" % (pull_request_index, documents))

    all_mergers: list[str] = get_all_mergers(es, pull_request_index)
    merger_data: list[dict[str, Any]] = []
    for user_login in all_mergers:
        try:
            user_analysis: dict[str, Any] = analyse_user(es, pull_request_index, user_login, variables,
                                                         calendar_interval,
                                                         information_criterion, pull_request_index)
            if user_analysis:
                merger_data.append(user_analysis)
        except Exception:
            logging.error(traceback.format_exc())
            logging.error("Cannot analyse user %s" % user_login)

    consolidated_analysis: pd.DataFrame = pd.DataFrame(merger_data)
    return documents, consolidated_analysis
